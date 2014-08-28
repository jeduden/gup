from __future__ import print_function
import sys
import logging
import optparse
import os

from .error import *
from .util import *
from .state import TargetState, AlwaysRebuild, Checksum, FileDependency, META_DIR
from .gupfile import Builder
from .log import PLAIN, getLogger, TRACE_LVL
from .var import INDENT, set_verbosity, DEFAULT_VERBOSITY, set_trace, PY3, IS_WINDOWS
from .parallel import setup_jobserver
from .task import Task, TaskRunner
from .version import VERSION

_log = getLogger(__name__)

def _init_logging(verbosity):
	lvl = logging.INFO
	fmt = '%(color)sgup  ' + INDENT + '%(bold)s%(message)s' + PLAIN

	if verbosity < 0:
		lvl = logging.ERROR
	elif verbosity == 1:
		lvl = logging.DEBUG
	elif verbosity > 1:
		fmt = '%(color)sgup[%(process)s %(name)-12s %(levelname)-5s]  ' + INDENT + '%(bold)s%(message)s' + PLAIN
		lvl = TRACE_LVL
	
	if 'GUP_IN_TESTS' in os.environ:
		lvl = TRACE_LVL
		fmt = fmt = '# %(color)s%(levelname)-5s ' + INDENT + '%(bold)s%(message)s' + PLAIN

	# persist for child processes
	set_verbosity(verbosity)

	baseLogger = getLogger('gup')
	handler = logging.StreamHandler()
	handler.setFormatter(logging.Formatter(fmt))
	baseLogger.propagate = False
	baseLogger.setLevel(lvl)
	baseLogger.addHandler(handler)

def _bin_init():
	'''
	Ensure `gup` is present on $PATH
	'''
	progname = sys.argv[0]
	_log.trace('run as: %s' % (progname,))
	if os.environ.get('GUP_IN_PATH', '0') != '1':
		# only do this check once
		os.environ['GUP_IN_PATH'] = '1'

		# XXX on non-Windows OSes, recursive invocations may not find `gup`
		# if the toplevel was found via a "." entry on $PATH. Let's assume
		# only Windows is dumb enough to do that.
		if IS_WINDOWS or os.path.sep in progname:
			# gup may have been run as a relative / absolute script - check
			# whether our directory is in $PATH
			here, filename = os.path.split(__file__)
			if filename.startswith('cmd.py'):
				# we're being run in-place
				_log.trace("Run from gup/ package - assuming gup in $PATH")
			else:
				path_entries = os.environ.get('PATH', '').split(os.pathsep)
				for entry in path_entries:
					if not entry: continue

					# If we're found via a relative entry (like ".") on
					# $PATH, we can't rely on that:
					if not os.path.isabs(entry): continue

					try:
						if samefile(entry, here):
							_log.trace('found `gup` in $PATH')
							# ok, we're in path
							break
					except OSError: pass
				else:
					# not found
					here = os.path.abspath(here)
					_log.trace('`gup` not in $PATH - adding %s' % (here,))
					os.environ['PATH'] = os.pathsep.join([here] + path_entries)

def _main(argv):
	p = None
	action = None

	try:
		cmd = argv[0]
	except IndexError:
		pass
	else:
		if cmd == '--clean':
			p = optparse.OptionParser('Usage: gup --clean [OPTIONS] [dir [...]]')
			p.add_option('-i', '--interactive', action='store_true', help='Ask for confirmation before removing files', default=False)
			p.add_option('-n', '--dry-run', action='store_false', dest='force', help='Just print files that would be removed')
			p.add_option('-f', '--force', action='store_true', help='Actually remove files')
			p.add_option('-m', '--metadata', action='store_true', help='Remove .gup metadata directories, but leave targets')
			action = _clean_targets
		elif cmd == '--contents':
			p = optparse.OptionParser('Usage: gup --contents')
			action = _mark_contents
		elif cmd == '--always':
			p = optparse.OptionParser('Usage: gup --always')
			action = _mark_always
		elif cmd == '--ifcreate':
			p = optparse.OptionParser('Usage: gup --ifcreate [file [...]]')
			action = _mark_ifcreate
		elif cmd == '--buildable':
			p = optparse.OptionParser('Usage: gup --buildable')
			action = _test_buildable
		elif cmd == '--features':
			p = optparse.OptionParser('Usage: gup --features')
			action = _list_features
	
	if action is None:
		# default parser
		p = optparse.OptionParser('Usage: gup [action] [OPTIONS] [target [...]]\n\n' +
			'Actions: (if present, the action must be the first argument)\n'
			'  --always     Mark this target as always-dirty\n' +
			'  --ifcreate   Rebuild the current target if the given file(s) are created\n' +
			'  --contents   Checksum the contents of stdin\n' +
			'  --clean      Clean any gup-built targets\n' +
			'  (use gup <action> --help) for further details')

		p.add_option('-u', '--update', '--ifchange', dest='update', action='store_true', help='Only rebuild stale targets', default=False)
		p.add_option('-j', '--jobs', type='int', default=None, help="Number of concurrent jobs to run")
		p.add_option('-x', '--trace', action='store_true', help='Trace build script invocations (also sets $GUP_XTRACE=1)')
		p.add_option('-q', '--quiet', action='count', default=0, help='Decrease verbosity')
		p.add_option('-v', '--verbose', action='count', default=DEFAULT_VERBOSITY, help='Increase verbosity')
		action = _build
		verbosity = None
	else:
		argv.pop(0)
		verbosity = 0

	opts, args = p.parse_args(argv)

	if verbosity is None:
		verbosity = opts.verbose - opts.quiet

	_init_logging(verbosity)
	_bin_init()

	_log.trace('argv: %r, action=%r', argv, action)
	args = [arg.rstrip(os.path.sep) for arg in args]
	action(opts, args)

def _get_parent_target():
	t = os.environ.get('GUP_TARGET', None)
	if t is not None:
		assert os.path.isabs(t)
	return t

def _assert_parent_target(action):
	p = _get_parent_target()
	if p is None:
		raise SafeError("%s was used outside of a gup target" % (action,))
	return p

def _mark_always(opts, targets):
	assert len(targets) == 0, "no arguments expected"
	parent_target = _assert_parent_target('--always')
	TargetState(parent_target).add_dependency(AlwaysRebuild())

def _mark_ifcreate(opts, files):
	assert len(files) > 0, "at least one file expected"
	parent_target = _assert_parent_target('--ifcreate')
	parent_state = TargetState(parent_target)
	for filename in files:
		if os.path.lexists(filename):
			raise SafeError("File already exists: %s" % (filename,))
		parent_state.add_dependency(FileDependency.relative_to_target(parent_target, mtime=None, checksum=None, path = filename))

def _test_buildable(opts, args):
	assert len(args) == 1, "exactly one argument expected"
	target = args[0]
	if Builder.for_target(target) is None:
		sys.exit(1)

def _list_features(opts, args):
	assert len(args) == 0, "no arguments expected"
	for feature in [
		'version ' + VERSION,
	]:
		print(feature)

def _mark_contents(opts, targets):
	parent_target = _assert_parent_target('--contents')
	if len(targets) == 0:
		assert not sys.stdin.isatty()
		checksum = Checksum.from_stream(sys.stdin.buffer if PY3 else sys.stdin)
	else:
		checksum = Checksum.from_files(targets)
	TargetState(parent_target).add_dependency(checksum)

def _clean_targets(opts, dests):
	if opts.force is None:
		raise SafeError("Either --force (-f) or --dry-run (-n) must be given")

	def rm(path, isdir=False):
		if not opts.force:
			print("Would remove: %s" % (path))
			return

		print("Removing: %s" % (path,), file=sys.stderr)
		if opts.interactive:
			print("   [Y/n]: ", file=sys.stderr, end='')
			if raw_input().strip() not in ('','y','Y'):
				print("Skipped.", file=sys.stderr)
				return

		if not isdir:
			try:
				os.remove(path)
				return
			except OSError:
				pass
		rmtree(path)

	if len(dests) == 0: dests = ['.']
	for dest in dests:
		for dirpath, dirnames, filenames in os.walk(dest, followlinks=False):
			if META_DIR in dirnames:
				gupdir = os.path.join(dirpath, META_DIR)
				if not opts.metadata:
					deps = TargetState.built_targets(gupdir)
					for dep in deps:
						if dep in (filenames + dirnames):
							target = os.path.join(dirpath, dep)
							if Builder.for_target(target) is not None:
								rm(target)
				rm(gupdir, isdir=True)
			# filter out hidden directories
			hidden_dirs = [d for d in dirnames if d.startswith('.')]
			for hidden in hidden_dirs:
				dirnames.remove(hidden)

def _build(opts, targets):
	if opts.trace:
		set_trace()
	
	if len(targets) == 0:
		targets = ['all']

	parent_target = _get_parent_target()

	jobs = opts.jobs
	if jobs is not None:
		assert jobs > 0 and jobs < 1000
	setup_jobserver(jobs)

	runner = TaskRunner()
	for target_path in targets:
		if os.path.abspath(target_path) == parent_target:
			raise SafeError("Target `%s` attempted to build itself" % (target_path,))

		task = Task(opts, parent_target, target_path)
		target = task.prepare()
		if target is not None:
			# only add a task if it's a buildable target
			runner.add(task)
		else:
			# otherwise, perform post-build actions (like updating parent dependencies)
			task.complete()

	# wait for all tasks to complete
	runner.run()

def _exit_error():
	sys.exit(2)

def main():
	try:
		_main(sys.argv[1:])
	except KeyboardInterrupt:
		sys.exit(2)
	except SafeError as e:
		if len(e.args) > 0:
			_log.error("%s" % (str(e),))
		sys.exit(2)

if __name__ == '__main__':
	main()
