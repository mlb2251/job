
import sys,os
import argparse
import shlex
import time
from subprocess import CalledProcessError
import datetime
import mlb
from mlb import die
import pathlib
import libtmux
import re
from collections import defaultdict
from fastcore.utils import run


BASE_DIR = "/scratch/mlbowers/proj/ec"
BASE_CMD = "python bin/matt.py"
time_str = datetime.datetime.now().strftime('%m-%d.%H-%M-%S') 


parser = argparse.ArgumentParser()

modes = { # modes and valid argcounts
  'new':(1,),
  'diff':(2,),
  'edit':(1,),
  'run':(1,),
  'rename':(2,),
  'kill':(1,),
  'view':(1,),
  'ls':(0,),
  'copy':(2,),
  'del':(1,),
}

parser.add_argument('mode',
                    type=str,
                    help='operation to run')
parser.add_argument('subargs',
                    type=str,
                    nargs='*',
                   help='args to subcommand')
parser.add_argument('-f',
                    action='store_true',
                    help='force to kill existing session by same name if it exists')
#parser.add_argument('--no-name',
#                    action='store_true',
#                    help='suppress inserting name=[window name] at the end of the command')

args = parser.parse_args()

# figure out our directory paths
root_dir = pathlib.Path(__file__).parent.parent.absolute() # the top level git diretory for job/
jobs_dir = root_dir / 'jobs'
trash_dir = root_dir / 'trash'

# initialize any dirs necessary
assert root_dir.is_dir()
if not jobs_dir.is_dir():
    jobs_dir.mkdir()
if not trash_dir.is_dir():
    trash_dir.mkdir()

# deal with abbvs (allow any unique prefix of a real command)
if args.mode not in modes:
  abbv = args.mode
  full = None
  for mode in modes:
    if mode.startswith(abbv):
      if full is not None:
        die(f'mode {abbv} ambiguously specifies both {full} and {mode}')
      full = abbv
  if full is None:
    die(f'mode {abbv} is not a valid mode or abbreviation. Modes: {modes}')
  args.mode = full

# check that a valid number of args was given
if len(args.subargs) not in modes[args.mode]:
  die(f'invalid number of args ({len(args.subargs)}) for command {args.mode} which accepts any of the following number of args: {modes[args.mode]}')

server = libtmux.Server()


def replace_self(cmd):
  """
  use os.execlv to replace the current process with a new one.
  `cmd` can be a string (which gets shlexed) or a list of cmd + args

  execvp is used bc it has `p` which means it uses the $PATH to find what program ur referring to
  and it has `v` which means it takes the list of args as a second argument. Btw the list
  of args should include the program name or you'll get a weird error, which is why
  I send in the whole `cmd` as the args
  """
  if isinstance(cmd,str):
    cmd = shlex.split(cmd)
  file = cmd[0]
  os.execvp(file,cmd)


def jobfile(job_name):
    return jobs_dir/job_name

def processidentifier(job_name):
  return f'job_id={job_name}___jobid___'

def get_processess(job_name):
  """
  return a possibly empty list of processes owned by this job
  Specifically returns a list of tuples of the form (pid:int, cmd:str)
  """
  procid = processidentifier(job_name)
  try:
    # -a just makes it display more than just the pid
    lines = run(f'pgrep -a -u {os.environ["USER"]} --full {procid}').split('\n')
  except OSError:
      return [] # pgrep found nothing
  lines = [l.strip() for l in lines if l.strip()!='']
  assert len(lines) != 0, "I think pgrep should error out instead of this happening"
  # list of (pid,cmd) tuples, one per process
  pid_cmd_list = [(int(line.split(' ')[0]),' '.join(line.split(' ')[1:])) for line in lines]
  return pid_cmd_list

def get_session(job_name):
    """
    get the tmux session for a job if it exists (else None)
    """
    results = server.where(dict(session_name=job_name))
    if len(results) == 0:
      return None
    if len(results) == 1:
      return results[0]
    if len(results) > 1:
      die(f"Im confused, there shouldnt be two sessions w the same name: {results}")

def kill_session_and_processess(job_name):
    """
    kill all tmux sessions and all processes associated with this job
    """
    sess = get_session(job_name)
    if sess is not None:
      print(f"killing session {job_name}")
      sess.kill_session()
    procs = get_processess(job_name):
    for (pid,cmd) in procs:
      print(f"killing process {pid}: {cmd}")
    if len(procs) > 0:
      # im too scared so instead of using the pids from get_processes
      # i just use pkill which has `-u` to again guarantee im only killing my own
      # processess
      run(f'pkill -u {os.environ["USER"]} --full {processidentifier(job_name)}')

def sorted_jobs():
    """
    list of names of jobs in jobs folder sorted by last modified time
    """
    jobs = []
    for jobfile in jobs_dir.iterdir():
      job_name = jobfile.name
      jobs.append((job_name,jobfile.stat().st_mtime)) # get last modified time (float: time since unix epoch)
    jobs = sorted(jobs, key=lambda x:x[1])
    return [job[0] for job in jobs] # strip out the modification time

def launch_view(job_name):
    sess = get_session(job_name)
    if sess is None:
        die(f"Can't find session {job_name}")
    replace_self(f'tmux a -t {job_name}')


class JobParser:
  def __init__(self,job_name) -> None:
    self.job_name = job_name
    self.file = jobfile(job_name)
    self.windows = {} # str -> str
    self.shared_local = {} # str -> str
    self.shared_global = ''
    self.params = defaultdict(dict) # param_name:str -> param_variant:str -> effect:str
    self.lineno = 0

    self.sess = None


    if not self.file.exists():
        die(f"Job '{job_name}' doesn't exist")

  def start(self):
    try:
        self.parse()
    except Exception as e:
        self.error(f'{e}')
    self.launch_session()
    for run_name,cmd in self.windows.items():
        self.launch_window(run_name,cmd)
    launch_view(self.job_name)

  def launch_session(self):
    print(f"Launching session: {self.job_name}")
    # tmux new-session -d -s @(sess_name)
    if get_session(self.job_name) is not None:
        # session exists
        if not args.f:
            die(f'{self.job_name} is already running, add `-f` to kill')
        kill_session_and_processess(self.job_name)
        assert get_session(self.job_name) is None

    # make new session in proj/ec with job_name as name
    sess = server.new_session(self.job_name, attach=False, start_directory=BASE_DIR, window_name='info')
    # make the first window of the session an "info" pane with the job details
    sess.windows[0].panes[0].send_keys(f'cat {jobfile(self.job_name)}',suppress_history=False)
    self.sess = sess

  def launch_window(self, run_name:str, cmd:str):
    """
    Add a new tmux window named `run_name` to existing tmux session `self.job_name` and
        send it command `cmd` then hit enter.
    Send cmd=None to simply create the window without sending a command
    """
    assert self.sess is not None
    print(f"* Launching window {run_name}: {cmd}")
    # make window named run_name
    window = self.sess.new_window(attach=False, window_name=run_name, start_directory=BASE_DIR)

    if cmd is None:
        return

    # send and execute the command. It'll hit <CR> for us.
    
    window.panes[0].send_keys(cmd,suppress_history=False)

  def add_window(self, name:str, cmd:str):
      if name in self.windows:
          self.error(f'window name used twice: {name}')
      if name.startswith(self.job_name):
          self.error(f'run name {name} starts with job name {self.job_name} which isnt allowed in tmux')
      self.windows[name] = cmd

  def add_param(self, param:str, variant:str, cmd:str):
      if param in self.params and variant in self.params[param]:
          self.error(f'param variant "{param}={variant}" already exists')
      self.params[param][variant] = cmd
  def get_shared(self):
      return ' '.join(self.shared_local.values()) + self.shared_global

  def error(self,s):
    mlb.red(f'Error parsing "{self.job_name}" line {self.lineno}: {self.line}')
    die(f'Error: {s}')

  def parse_variants(self,args):
    cmd = ''
    variants = []
    for arg in args:
        if '=' not in arg:
            self.error(f'each space separated argument to `test` should have an equals sign in it but this doesnt: {arg}')
        param,variant = arg.split('=')
        if param not in self.params:
            self.error(f'unable to find param `{param}` when parsing the argument {arg} to `test` (are you sure you defined it with `param`?)')
        if variant not in self.params[param]:
            self.error(f'unable to find variant `{variant}` for param `{param}` when parsing the argument {arg} to `test` (are you sure you defined it with `param`?)')
        cmd += ' ' + self.params[param][variant]
        variants.append(variant)
    run_name = '.'.join(variants)
    return run_name, cmd

  def parse(self):
    """
    Parse `self.file` and return a window_name -> 


    parse the sessions in the job file of the given name and return a dict of {window_name:cmd}
    """
    for self.lineno,self.line in enumerate(open(self.file,'r'),start=1):

        line = line.strip()
        if line == '':
            continue # empty line
        if line.startswith('#'):
            continue # comment

        mode,*args = [l for l in line.split(' ') if l != '']

        if mode == 'param': # define a parameter
            if len(args) < 2:
                self.error(f"invalid number of arguments to `param`")
            param = args[0]
            variant = args[1]
            cmd = ' '.join(args[2:])
            self.add_param(param,variant,cmd)
            continue

        elif mode.startswith('shared'): # set some shared args
            cmd = ' '.join(args)
            if '(' in mode: # "shared(4)" syntax
                key = mode[mode.index('(')+1: mode.index(')')].strip()
                self.shared_local[key] = cmd
            else:
                self.shared_global += f' {cmd}'
            continue

        elif mode == 'raw': # launch a verbatim bash command where first argument is the window name
            if len(args) == 0:
                self.error('missing window name for raw command')
            win_name = args[0]
            cmd = ' '.join(args[1:])
            self.add_window(win_name,cmd)
            continue

        elif mode == 'run': # launch a train/test run
            run_name, from_params = self.parse_variants(args)
            shared = self.get_shared()
            cmd = f'{CHDIR} && $[{BASE_CMD} job_name={self.job_name} run_name={run_name} {processidentifier(self.job_name)} {shared} {from_params} job_info={time_str}.{self.job_name}.{run_name}]'
            self.add_window(run_name,cmd)
            continue

        else:
            self.error(f"unrecognized command {mode}")
        assert False

    print(f"Parsed {len(self.windows)} windows")
    return


session = args.name
if args.mode == 'new':
    if job_exists(session):
        die(f"A job named {session} already exists, you may want to edit it or delete it")
    vim @(get_job_file(session))
    print(f"[Created job file for {session}]")
    sys.exit(0)
elif args.mode == 'diff':
    if args.name2 is None:
        die("please use the syntax `job diff job1 job2`")
    fst = args.name
    snd = args.name2
    if not job_exists(fst):
        die(f"can't find job {fst}")
    if not job_exists(snd):
        die(f"can't find job {snd}")
    vimdiff @(get_job_file(fst)) @(get_job_file(snd))
    sys.exit(0)
elif args.mode == 'rename':
    if args.name2 is None:
        die("please use the syntax `job rename old new`")
    src = args.name
    dst = args.name2
    if not job_exists(src):
        die(f"can't find job {src}")
    if job_exists(dst):
        die(f"job already exists {dst}")
    mv @(get_job_file(src)) @(get_job_file(dst))
    print(f"[Renamed job {src} -> {dst}]")
    sys.exit(0)
elif args.mode == 'copy':
    if args.name2 is None:
        die("please use the syntax `job copy source target`")
    src = args.name
    dst = args.name2
    if not job_exists(src):
        die(f"can't find job {src}")
    if job_exists(dst):
        die(f"job already exists {dst}")
    cp @(get_job_file(src)) @(get_job_file(dst))
    vim @(get_job_file(dst))
    print(f"[Updated job file for {dst}]")
    sys.exit(0)
elif args.mode == 'edit':
    if not job_exists(session):
        die(f"No job named {session} exists")
    vim @(get_job_file(session))
    print(f"[Updated job file for {session}]")
    sys.exit(0)
elif args.mode == 'run':
    # handle removing any running session
    if session_exists(session):
        if not args.f:
            die(f"tmux session `{session}` exists. Run with -f to force replacing this session")
        kill(session)
    # launch session
    new_session(session)
    # launch windows
    windows = parse_job(session)
    new_windows(session,windows)
    view_session(session)
    sys.exit(0)
elif args.mode == 'plot':
    die(f"not totally implemented yet, or maybe it works idk")
    _,plots = parse_job(session)
    pushd $HOME/proj/ec
    for plot in plots:
        print(f"Plotting {plot_name}")
        load= '___'.join([session+'.'+name for name in plot.names]) # put in prefix.name format with triple underscores
        echo python bin/test_list_repl.py mode=plot plot.title=@(plot.plot_name) load=@(load) plot.suffix=@(plot.suffix)
        python bin/test_list_repl.py mode=plot plot.title=@(plot.plot_name) load=@(load) plot.suffix=@(plot.suffix)
        print(f"Plotted")
    popd
    sys.exit(0)
elif args.mode == 'kill':
    kill(session)
    sys.exit(0)
elif args.mode == 'view':
    view_session(session)
    sys.exit(0)
elif args.mode == 'del':
    kill(session)
    if not job_exists(session):
        die("job doesnt exist")
    get_job_file(session).rename(trash_dir / session)
    print(f"moved {session} to trash")
elif args.mode == 'ls':
    for name in sorted_jobs():
        if session_exists(name):
            mlb.green(f"{name}") # print in green if already running
        else:
            mlb.red(f"{name}") # else print normally
    sys.exit(0)






