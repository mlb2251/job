
import sys,os
import argparse
import shlex
import shutil
import time
from subprocess import CalledProcessError
import mlb
import pathlib
import libtmux
import re
import contextlib
from collections import defaultdict
from fastcore.utils import run
from mlb.color import yellow
from datetime import datetime, timezone

def die(s):
    mlb.red(s)
    sys.exit(1)


BASE_DIR = "/scratch/mlbowers/proj/ec"
BASE_CMD = "bin/matt.py"
time_str = datetime.now().strftime('%m-%d.%H-%M-%S') 


parser = argparse.ArgumentParser()

modes = { # modes and valid subarg counts
  'new':(1,),
  'edit':(1,),
  'run':(1,),
  'ls':(0,),
  'diff':(2,),
  'mv':(2,),
  'kill':(1,),
  'view':(1,),
  'cp':(2,),
  'del':(1,),
  'file':(1,),
}

parser.add_argument('mode',
                    type=str,
                    help='operation to run')
parser.add_argument('subargs',
                    type=str,
                    nargs='*',
                   help='args to subcommand')
parser.add_argument('--force',
                    action='store_true',
                    help='force to kill existing session by same name if it exists')
parser.add_argument('--first',
                    action='store_true',
                    help='only launch the first (non-info) window of the session (used to test)')

jobpy_args = parser.parse_args()

# figure out our directory paths
root_dir = pathlib.Path(__file__).parent.parent.absolute() # the top level git diretory for job/
jobs_dir = root_dir / 'jobs'
trash_dir = root_dir / 'trash'

# initialize any dirs necessary
assert root_dir.is_dir()
jobs_dir.mkdir(exist_ok=True)
trash_dir.mkdir(exist_ok=True)

# deal with abbvs (allow any unique prefix of a real command)
mode = jobpy_args.mode
if mode not in modes:
  full = None
  for _mode in modes:
    if _mode.startswith(mode):
      if full is not None:
        die(f'mode {mode} ambiguously specifies both {full} and {_mode}')
      full = _mode
  if full is None:
    die(f'mode {mode} is not a valid mode or abbreviation. Modes: {modes}')
  mode = jobpy_args.mode = full

# check that a valid number of args was given
if len(jobpy_args.subargs) not in modes[mode]:
  die(f'invalid number of args ({len(jobpy_args.subargs)}) for command {mode} which accepts any of the following number of args: {modes[mode]}')

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
    get the tmux session for a job if it exists (else None).
    This is very slow if you call it many times, so prefer `list_sessions()` in that case
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
    procs = get_processess(job_name)
    for (pid,cmd) in procs:
      print(f"killing process {pid}: {cmd}")
    if len(procs) > 0:
      # im too scared so instead of using the pids from get_processes
      # i just use pkill which has `-u` to again guarantee im only killing my own
      # processess
      run(f'pkill -u {os.environ["USER"]} --full {processidentifier(job_name)}')


def launch_view(job_name):
    sess = get_session(job_name)
    if sess is None:
        die(f"Can't find session {job_name}")
    replace_self(f'tmux a -t {job_name}')


class JobParser:
  def __init__(self,job_name) -> None:
    self.job_name = job_name
    self.file = jobfile(job_name)
    if not self.file.exists():
        die(f"Job '{job_name}' doesn't exist")
    self.windows = {} # str -> str
    self.shared_local = {} # str -> str
    self.shared_global = ''
    self.params = defaultdict(dict) # param_name:str -> param_variant:str -> effect:str
    self.lineno = 0
    self.sess = None

  def start(self):
    try:
        self.parse()
    except Exception as e:
        self.error(f'{e}')
    self.launch_session()
    for run_name,cmd in self.windows.items():
        self.launch_window(run_name,cmd)
        if jobpy_args.first: # just launch the first window and then execl into it
            launch_view(self.job_name)

  def launch_session(self):
    # tmux new-session -d -s @(sess_name)
    if get_session(self.job_name) is not None:
        # session exists
        if not jobpy_args.force:
            die(f'{self.job_name} is already running, add `--force` to kill')
        kill_session_and_processess(self.job_name)
        assert get_session(self.job_name) is None

    print(f"Launching session: {self.job_name}")
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

        line = self.line.strip()
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

        elif mode in ('run','vprof'): # launch a train/test run
            run_name, from_params = self.parse_variants(args)
            shared = self.get_shared()
            cmd = f'{BASE_CMD} job_name={self.job_name} run_name={run_name} {processidentifier(self.job_name)} {shared} {from_params} job_info={time_str}.{self.job_name}.{run_name}'
            if mode == 'run':
                cmd = f'![python {cmd}]'
            elif mode == 'vprof':
                cmd = f'![vprof -c cp "{cmd}" --output-file profile.json]'
            self.add_window(run_name,cmd)
            continue

        else:
            self.error(f"unrecognized command {mode}")
        assert False

    print(f"Parsed {len(self.windows)} windows")
    return

subargs = jobpy_args.subargs

def jobfile_checked(job_name, exists):
    if exists:
        job_name = search_jobnames(job_name)
        return jobfile(job_name)
    else:
        if jobfile(job_name).exists():
            die(f"Error: job {job_name} already exists")
        return jobfile(job_name)

def sorted_jobfiles():
    jobfiles = [p for p in jobs_dir.iterdir()]
    jobfiles.sort(key=lambda p: p.stat().st_mtime) # sort by last modified time
    return jobfiles # note jobname is just jobfile.name

def search_jobnames(job_name):
    if job_name.isdigit():
        # so if job_name == '3' we get the 3rd most recent job file returned by ls
        return sorted_jobfiles()[-int(job_name)].name
    jobnames = [j.name for j in sorted_jobfiles()]
    if job_name in jobnames:
        return job_name # exact match
    possible = [j.startswith(job_name) for j in jobnames]
    if len(possible) == 0:
        die(f"Error: can't find job {job_name}")
    if len(possible) == 1:
        return possible[0]
    else:
        die(f"Error: job name {job_name} matches multiple jobs: {possible}")

if mode == 'new':
    [job_name] = subargs
    file = jobfile_checked(job_name, exists=False)
    print(f"[Creating job file for {job_name}]")
    replace_self(f'vim {file}')

elif mode == 'diff':
    [job_name1, job_name2] = subargs
    file1 = jobfile_checked(job_name1, exists=True)
    file2 = jobfile_checked(job_name2, exists=True)
    replace_self(f'vimdiff {file1} {file2}')

elif mode == 'mv':
    [job_name_old, job_name_new] = subargs
    file_old = jobfile_checked(job_name_old,exists=True)
    file_new = jobfile_checked(job_name_new,exists=False)
    yellow('warning: only do this if you havent already launched the jobs or youre gonna relaunch them')
    file_old.rename(file_new)

    print(f"[Moved job {job_name_old} -> {job_name_new}]")
    sys.exit(0)

elif mode == 'cp':
    [job_name_old, job_name_new] = subargs
    file_old = jobfile_checked(job_name_old,exists=True)
    file_new = jobfile_checked(job_name_new,exists=False)

    shutil.copy(file_old,file_new)
    print(f"[Updated job file for {file_new}]")
    replace_self(f'vim {file_new}')

elif mode == 'edit':
    [job_name] = subargs
    file = jobfile_checked(job_name, exists=True)
    print(f"[Updated job file for {file}]")
    replace_self(f'vim {file}')

elif mode == 'run':
    [job_name] = subargs
    job_name = search_jobnames(job_name)
    p = JobParser(job_name)
    p.start()
    launch_view(job_name)

elif mode == 'kill':
    [job_name] = subargs
    job_name = search_jobnames(job_name)
    kill_session_and_processess(job_name)
    sys.exit(0)

elif mode == 'view':
    [job_name] = subargs
    job_name = search_jobnames(job_name)
    file = jobfile_checked(job_name, exists=True)
    launch_view(job_name)

elif mode == 'del':
    [job_name] = subargs
    job_name = search_jobnames(job_name)
    kill_session_and_processess(job_name)
    file = jobfile_checked(job_name,exists=True)
    file.rename(trash_dir / job_name)
    print(f"moved {job_name} -> {trash_dir / job_name}")
    sys.exit(0)

elif mode == 'file':
    [job_name] = subargs
    file = jobfile_checked(job_name,exists=True)
    print(file)
    sys.exit(0)



elif mode == 'ls':
    active_sessions = [sess.name for sess in server.sessions]
    jobfiles = sorted_jobfiles()

    for jobfile in jobfiles:
        job_name = jobfile.name
        active = job_name in active_sessions
        description = ''
        with jobfile.open(encoding='utf-8',errors='ignore') as f:
            line = f.readline().strip() # doesnt error out on EOF thankfully
            if line.startswith('#'):
                description = '\n\t' + line[1:].strip()
        last_modified = datetime.fromtimestamp(jobfile.stat().st_mtime, tz=timezone.utc).strftime('[%b %d %H:%M:%S]')
        colored_job_name = mlb.mk_green(job_name) if active else mlb.mk_red(job_name)
        print(f'{last_modified} {colored_job_name} {description}')

    sys.exit(0)


