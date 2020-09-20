import sys,os
import argparse
import time
from subprocess import CalledProcessError

if 'TMUX' in ${...}:
    # this is due to a bug where every `tmux` command including ones that dont normally
    # change the current window to tmux will actually open tmux
    # instead of just silently operating
    print("dont run this from within tmux")
    sys.exit(1)

parser = argparse.ArgumentParser()

parser.add_argument('mode',
                    type=str,
                    choices=['new','edit','run','kill','view'] + ['n','e','r','k','v']
                    help='operation to run')
parser.add_argument('name',
                    type=str,
                    help='experiment name (used to find the right file)')
parser.add_argument('-f',
                    action='store_true',
                    help='force to kill existing session by same name if it exists')
parser.add_argument('--no-name',
                    action='store_true',
                    help='suppress inserting name=[window name] at the end of the command')

args = parser.parse_args()
session = name

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


def session_exists(name):
    for line in $(tmux ls).split('\n'):
        if not ':' in line:
            continue # get to lines of the form "t2_d3_sept18: 4 windows (created Fri Sep 18 23:05:23 2020) [150x48]"
        if line.split(':')[0] == name: # found existing session by this name
            return True
    return False

def process_exists(prefix):
    lines=$(pgrep -a -u mlbowers --full prefix=@(prefix)).split('\n')
    lines = [l.strip() for l in lines if l.strip()!='']
    if len(lines) == 0:
        return False
    print("found processes:")
    for line in lines:
        print(f"\t{line}")
    return True

def kill(session):
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

if args.mode == 'new':
    raise NotImplementedError
elif args.mode == 'edit':
    raise NotImplementedError
elif args.mode == 'run':
    # handle removing any running sesion
    if session_exists(session):
        if not args.f:
            print(f"tmux session `{session}` exists. Run with -f to force replacing this session")
            sys.exit(1)
        kill(session)
    # launch session
    raise NotImplementedError
elif args.mode == 'kill':
    kill(session)
    sys.exit(0)
elif args.mode == 'view':
    raise NotImplementedError





sessions = {}

for line in open(args.file,'r'):
    line = line.strip()
    if line == '':
        continue
    if ':' not in line:
        print(f"Colon missing in line: {line}, aborting")
        sys.exit(1)
    name, *rest = line.split(':')
    rest = ':'.join(rest) # in case it had any colons in it
    rest = rest.strip()
    if not args.no_name:
        rest = f'cd ~/proj/ec && python bin/test_list_repl.py {rest} prefix={session} name={name}'
    sessions[name] = rest

print(f"launching session `{session}`")
tmux new-session -d -s @(session)

print("launching windows")
for i,(name,cmd) in enumerate(sessions.items()):
    print(f"\t{name}: {cmd}")
    # first make a new window with the right name
    tmux new-window -t @(session) -n @(name)
    # now send keys to the session (which will have the newly created window
    # active already so this will run in that new window
    tmux send-keys -t @(session) @(cmd) C-m
    # C-m is like <CR>
    time.sleep(1.01) # so that the hydra session gets a different name
print("done!")
cmd = f"tmux a -t {session}"
print(f"attach with {cmd}")
tmux a -t @(session)


