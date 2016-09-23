# These modules provides a set of functions to execute actions on a host.
# If the host is local, it's done direcly; if it's remote we log in via SSH.

import os
import socket
import shutil
import subprocess
import logging

from BroControl import py3bro
from BroControl import ssh_runner
from BroControl import util


# Copies src to dst, preserving permission bits, but does not clobber existing
# files/directories.
# Works for files and directories (recursive).
def install(src, dstdir, cmdout):
    if not os.path.lexists(src):
        cmdout.error("pathname not found: %s" % src)
        return False

    dst = os.path.join(dstdir, os.path.basename(src))
    if os.path.lexists(dst):
        # Do not clobber existing files/dirs (this is not an error)
        return True

    logging.debug("cp %s %s", src, dstdir)

    try:
        if os.path.isfile(src):
            shutil.copy2(src, dstdir)
        elif os.path.isdir(src):
            shutil.copytree(src, dst)
    except OSError:
        # Python 2.6 has a bug where this may fail on NFS. So we just
        # ignore errors.
        pass
    except IOError as err:
        cmdout.error("failed to copy: %s" % err)
        return False

    return True

# rsyncs paths from localhost to destination hosts.
def sync(nodes, paths, cmdout):
    result = True
    cmds = []
    for n in nodes:
        args = ["-rRl", "--delete", "--rsh=\"ssh -o ConnectTimeout=30\""]
        dst = ["%s:/" % util.format_rsync_addr(util.scope_addr(n.host))]
        args += paths + dst
        cmdline = "rsync %s" % " ".join(args)
        cmds += [(n, cmdline, "", None)]

    for (id, success, output) in run_localcmds(cmds):
        if not success:
            cmdout.error("rsync to %s failed: %s" % (util.scope_addr(id.host), "\n".join(output)))
            result = False

    return result


# Runs command locally and returns tuple (success, output)
# with success being true if the command terminated with exit code 0,
# and output being the combined stdout/stderr output of the command.
def run_localcmd(cmd, env="", inputtext=None, donotcaptureoutput=False):
    proc = _run_localcmd_init("single", cmd, env, donotcaptureoutput)
    return _run_localcmd_wait(proc, inputtext)

# Same as above but runs a set of local commands in parallel.
# Cmds is a list of (id, cmd, envs, inputtext) tuples, where id is
# an arbitrary cookie identifying each command.
# Returns a list of (id, success, output) tuples.
def run_localcmds(cmds):
    results = []
    running = []

    for (id, cmd, envs, inputtext) in cmds:
        proc = _run_localcmd_init(id, cmd, envs)
        running += [(id, proc, inputtext)]

    for (id, proc, inputtext) in running:
        (success, output) = _run_localcmd_wait(proc, inputtext)
        results += [(id, success, output)]

    return results

def _run_localcmd_init(id, cmd, env, donotcaptureoutput=False):

    if env:
        cmdline = env + " " + cmd
    else:
        cmdline = cmd

    logging.debug(cmdline)

    if donotcaptureoutput:
        stdout = None
    else:
        stdout = subprocess.PIPE

    # os.setsid makes sure that the child process doesn't receive our CTRL-Cs.
    proc = subprocess.Popen([cmdline], stdin=subprocess.PIPE, stdout=stdout,
                            stderr=subprocess.STDOUT, close_fds=True,
                            shell=True, preexec_fn=os.setsid)

    return proc

def _run_localcmd_wait(proc, inputtext):
    if inputtext and py3bro.using_py3:
        inputtext = inputtext.encode()

    (out, err) = proc.communicate(inputtext)
    rc = proc.returncode

    output = []
    if out:
        if py3bro.using_py3:
            out = out.decode()
        output = out.splitlines()

    for line in output:
        logging.debug("    > %s", line)

    logging.debug("exit status: %d", rc)

    return (rc == 0, output)


# FIXME: This is an ugly hack. The __del__ method produces
# strange unhandled exceptions in the child at termination
# of the main process. Not sure if disabling the cleanup
# altogether is a good thing but right now that's the
# only fix I can come up with.
def _emptyDel(self):
    pass
subprocess.Popen.__del__ = _emptyDel


# Returns a list of the IP addresses associated with local interfaces.
# For IPv6 addresses, zone_id and prefix length are removed if present.
def get_local_addrs(cmdout):
    try:
        # On Linux, ifconfig is often not in the user's standard PATH.
        proc = subprocess.Popen(["PATH=$PATH:/sbin:/usr/sbin ifconfig", "-a"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
        out, err = proc.communicate()
        success = proc.returncode == 0
    except OSError:
        success = False

    if success:
        localaddrs = []
        if py3bro.using_py3:
            out = out.decode()
        for line in out.splitlines():
            fields = line.split()
            if "inet" in fields or "inet6" in fields:
                addrfield = False
                for field in fields:
                    if field == "inet" or field == "inet6":
                        addrfield = True
                    elif addrfield and field != "addr:":
                        locaddr = field
                        # remove "addr:" prefix (if any)
                        if field.startswith("addr:"):
                            locaddr = field[5:]
                        # remove everything after "/" or "%" (if any)
                        locaddr = locaddr.split("/")[0]
                        locaddr = locaddr.split("%")[0]
                        localaddrs.append(locaddr)
                        break
    else:
        cmdout.info("cannot get list of local IP addresses")

        localaddrs = ["127.0.0.1", "::1"]
        try:
            addrinfo = socket.getaddrinfo(socket.gethostname(), None, 0, 0, socket.SOL_TCP)
        except Exception:
            addrinfo = []

        for ai in addrinfo:
            localaddrs.append(ai[4][0])

    return localaddrs


class Executor:
    def __init__(self, localaddrs, helperdir, timeout):
        self.sshrunner = ssh_runner.MultiMasterManager(localaddrs)
        self.helperdir = helperdir
        self.timeout = timeout

    def finish(self):
        self.sshrunner.shutdown_all()

    # Run commands in parallel on one or more hosts.
    #
    # cmds:  a list of the form: [ (node, cmd, args), ... ]
    #   where "cmd" is a string, "args" is a list of strings.
    # shell:  if True, then the "cmd" (and "args") will be interpreted by a
    #   shell.
    # helper:  if True, then the "cmd" will be modified to specify the full
    #   path to the broctl helper script.
    #
    # Returns a list of results: [(node, success, output), ...]
    #   where "success" is a boolean (True if command's exit status was zero),
    #   and "output" is a list of strings (stdout followed by stderr) or an
    #   error message if no result was received (this could occur upon failure
    #   to communicate with remote host, or if the command being executed
    #   did not finish before the timeout).
    def run_cmds(self, cmds, shell=False, helper=False):
        results = []

        if not cmds:
            return results

        dd = {}
        hostlist = []
        for nodecmd in cmds:
            host = nodecmd[0].addr
            if host not in dd:
                dd[host] = []
                hostlist.append(host)
            dd[host].append(nodecmd)

        nodecmdlist = []
        for host in hostlist:
            for bronode, cmd, args in dd[host]:
                if helper:
                    cmdargs = [os.path.join(self.helperdir, cmd)]
                else:
                    cmdargs = [cmd]

                if shell:
                    if args:
                        cmdargs = ["%s %s" % (cmdargs[0], " ".join(args))]
                else:
                    cmdargs += args

                nodecmdlist.append((bronode.addr, cmdargs))
                logging.debug("%s: %s", bronode.host, " ".join(cmdargs))

        for host, result in self.sshrunner.exec_multihost_commands(nodecmdlist, shell, self.timeout):
            nodecmd = dd[host].pop(0)
            bronode = nodecmd[0]
            if not isinstance(result, Exception):
                res = result[0]
                out = result[1].splitlines()
                err = result[2].splitlines()
                results.append((bronode, res == 0, out + err))
                logging.debug("%s: exit code %d", bronode.host, res)
            else:
                results.append((bronode, False, [str(result)]))

        return results

    # Run shell commands in parallel on one or more hosts.
    # cmdlines:  a list of the form [ (node, cmdline), ... ]
    #   where "cmdline" is a string to be interpreted by the shell
    #
    # Return value is same as run_cmds.
    def run_shell_cmds(self, cmdlines):
        cmds = [ (node, cmdline, []) for node, cmdline in cmdlines ]

        return self.run_cmds(cmds, shell=True)

    # A convenience function that calls run_cmds.
    def run_helper(self, cmds, shell=False):
        return self.run_cmds(cmds, shell, True)

    # A convenience function that calls run_cmds.
    # dirs:  a list of the form [ (node, dir), ... ]
    #
    # Returns a list of the form: [ (node, success), ... ]
    #   where "success" is a boolean (true if specified directory was created
    #   or already exists).
    def mkdirs(self, dirs):
        results = []
        cmds = []

        for (node, dir) in dirs:
            cmds += [(node, "mkdir", ["-p", dir])]

        for (node, success, output) in self.run_cmds(cmds):
            results += [(node, success)]

        return results

    # A convenience function that calls run_cmds to remove directories
    # on one or more hosts.
    # dirs:  a list of the form [ (node, dir), ... ]
    #
    # Returns a list of the form: [ (node, success), ... ]
    #   where "success" is a boolean (true if specified directory was removed
    #   or does not exist).
    def rmdirs(self, dirs):
        results = []
        cmds = []

        for (node, dir) in dirs:
            cmds += [(node, "if [ -d %s ]; then rm -rf %s ; fi" % (dir, dir), [])]

        for (node, success, output) in self.run_cmds(cmds, shell=True):
            results += [(node, success)]

        return results

    def host_status(self):
        return self.sshrunner.host_status()

