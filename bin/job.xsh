import sys,os
import argparse
import time
from subprocess import CalledProcessError
import mlb
import pathlib
import re

BASE_CMD = "cd ~/proj/ec && python bin/test_list_repl.py"


parser = argparse.ArgumentParser()

parser.add_argument('mode',
                    type=str,
                    choices=['new','edit','run','kill','view','ls','copy','del','plot'] + ['n','e','r','k','v'],
                    help='operation to run')
parser.add_argument('name',
                    type=str,
                    default=None,
                    nargs='?',
                   help='experiment name (used to find the right file)')
parser.add_argument('name2',
                    type=str,
                    default=None,
                    nargs='?',
                   help='second experiment name used by some commands')
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

assert root_dir.is_dir()
if not jobs_dir.is_dir():
    jobs_dir.mkdir()
if not trash_dir.is_dir():
    trash_dir.mkdir()

# deal with abbvs
if args.mode == 'n':
    args.mode = 'new'
elif args.mode == 'e':
    args.mode = 'edit'
elif args.mode == 'r':
    args.mode = 'run'
elif args.mode == 'k':
    args.mode = 'kill'
elif args.mode == 'v':
    args.mode = 'view'

def die(msg):
    mlb.red(msg)
    sys.exit(1)

if 'TMUX' in ${...}:
    # this is due to a bug where every `tmux` command including ones that dont normally
    # change the current window to tmux will actually open tmux
    # instead of just silently operating
    die("Dont run this from within tmux")

# modes where it's okay to not have a job name
if args.mode not in ['ls']:
    if args.name is None:
        parser.print_help()
        die(f"Please provide a job name")
if args.mode not in ['copy']:
    if args.name2 is not None:
        parser.print_help()
        die(f"only provide one name please")

def get_job_file(name):
    return jobs_dir/name

def job_exists(name):
    """
    returns True if a job run config file exists with this name
    """
    return get_job_file(name).exists()

def session_exists(name):
    """
    returns True if a tmux session of this name is running
    """
    for line in $(tmux ls).split('\n'):
        if not ':' in line:
            continue # get to lines of the form "t2_d3_sept18: 4 windows (created Fri Sep 18 23:05:23 2020) [150x48]"
        if line.split(':')[0] == name: # found existing session by this name
            return True
    return False

def process_exists(prefix):
    """
    returns True if at least one process with a command that contains the string f'prefix={prefix}' is running
    """
    try:
        # -a just makes it display more than just the pid
        lines=$(pgrep -a -u mlbowers --full prefix=@(prefix)).split('\n')
    except CalledProcessError:
        return False # pgrep found nothing
    lines = [l.strip() for l in lines if l.strip()!='']
    assert len(lines) != 0, "I think pgrep should error out instead of this happening"
    print("Found processes:")
    for line in lines:
        print(f"\t{line}")
    return True

def kill(session):
    """
    kill all tmux session and all processes with the given name
    """
    if session_exists(session):
        tmux kill-session -t @(session)
        print(f"killed tmux session `{session}`")
    else:
        print("no sessions to kill")
    if process_exists(session):
        pkill -u mlbowers --full prefix=@(session)
        print("killed processes")
    else:
        print("no processes to kill")



def sorted_jobs():
    """
    list of jobs in jobs folder sorted by last modified time
    """
    jobs = []
    for name in jobs_dir.iterdir():
        jobs.append((name.name,os.path.getmtime(get_job_file(name.name)))) # get last modified time (float: time since unix epoch)
    jobs = sorted(jobs, reverse=True, key=lambda x:x[1])
    return [job[0] for job in jobs] # strip out the modification time

def view_session(session):
    if not session_exists(session):
        die(f"Can't find session {session}")
    tmux a -t @(session)

class Plot:
    def __init__(self,name):
        self.plot_name = name
        self.names = []
def parse_job(name, return_plots=False):
    """
    parse the sessions in the job file of the given name and return a dict of {window_name:cmd}
    """
    if not job_exists(name):
        die(f"Job {name} doesn't exist")
    file = get_job_file(name)
    assert file.exists(), "should never happen"

    shared = {}
    vars = {}
    plots = []
    in_plot = None
    mangle = True
    
    windows = {}
    for line in open(file,'r'):
        line = line.strip()
        if line == '':
            continue # empty line
        if line.startswith('#'):
            continue # comment
        if line.startswith('!'):
            metacmd,*args = line[1:].split(' ')
            args = [a for a in args if a != '']
            if metacmd.startswith('shared'):
                if '(' in metacmd: # "shared(4)" syntax
                    key = metacmd[metacmd.index('(')+1: metacmd.index(')')]
                else:
                    key = ''
                args = ' '.join(args).strip()
                shared[key] = args
            elif metacmd == 'var':
                vars[args[0]] = ' '.join(args[1:]).strip()
            elif metacmd == 'mangle':
                args = ' '.join(args).strip()
                if args not in ['on','off']:
                    die(f"!mangle expects argument 'on' or 'off' not {mangle}")
                mangle = (args == 'on')
            elif metacmd == 'plot':
                if len(args) == 0:
                    in_plot = None # exit plotting
                    continue
                in_plot = Plot(args[0],args[1])
                if any([p.plot_name == args[0] for p in plots]):
                    die(f"You reused the plot title {plot_title} in !plot directives")
                plots.append(in_plot) # create a new plot
            else:
                die(f"unrecognized metacommand: {metacmd}")
            continue
        
        if ':' not in line:
            die(f"Colon missing in line: {line}, aborting")
        
        curr_shared = ' '.join(list(shared.values()))

        win_name, *cmd = line.split(':')
        if win_name.startswith(session):
            die(f"run name {win_name} starts with the session name {session} which is not allowed bc it creates weird tmux issues")
        cmd = ':'.join(cmd) # in case it had any colons in it
        cmd = cmd.strip()
        while '$!' in cmd:
            # regex matching something like $!my_var_name_2_woo
            match = re.search(r'\$!\w+',cmd)
            if match is None:
                die(f"Theres a $! in this command but no variable name: {cmd}")
            var_name = match.group()[2:]
            if var_name not in vars:
                die(f"Var not found: {var_name} while parsing command: {cmd}")
            cmd = cmd.replace(match.group(),vars[var_name])
        if win_name in windows:
            die(f"You reused the same window name: {win_name}")
        if mangle:
            cmd = f'{BASE_CMD} {cmd} prefix={name} name={win_name}'
        windows[win_name] = f'{cmd} {curr_shared}'
        if in_plot is not None:
            plots[in_plot].append(win_name)
    print(f"Parsed {len(windows)} windows")
    if return_plots:
        return windows,plots
    return windows

def new_session(sess_name):
    print(f"Launching session: {sess_name}")
    tmux new-session -d -s @(sess_name)

def new_window(sess_name,win_name,cmd=None):
    print(f"* Launching window {win_name}: {cmd}")
    # first make a new window with the right name
    tmux new-window -t @(sess_name) -n @(win_name)
    if cmd is not None:
        # now send keys to the session (which will have the newly created window
        # active already so this will run in that new window)
        # (Note: C-m is like <CR>)
        tmux send-keys -t @(sess_name) @(cmd) C-m

def new_windows(sess_name, windows):
    for i,(win_name,cmd) in enumerate(windows.items()):
        new_window(sess_name, win_name, cmd)
        time.sleep(1.01) # so that the hydra session gets a different name



session = args.name
if args.mode == 'new':
    if job_exists(session):
        die(f"A job named {session} already exists, you may want to edit it or delete it")
    vim @(get_job_file(session))
    print(f"[Created job file for {session}]")
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
