import os, sys
import logging
from .var import IS_WINDOWS

# By default, no output colouring.
RED    = ""
GREEN  = ""
YELLOW = ""
BOLD   = ""
PLAIN  = ""

_want_color = os.environ.get('GUP_COLOR', 'auto')
if _want_color == '1' or (
			_want_color == 'auto' and
			not IS_WINDOWS and
			sys.stderr.isatty() and
			(os.environ.get('TERM') or 'dumb') != 'dumb'
		):
	# ...use ANSI formatting codes.
	RED    = "\x1b[31m"
	GREEN  = "\x1b[32m"
	YELLOW = "\x1b[33m"
	BOLD   = "\x1b[1m"
	PLAIN  = "\x1b[m"

_colors = {
	logging.INFO: GREEN,
	logging.WARN: YELLOW,
	logging.ERROR: RED,
	logging.CRITICAL: RED,
}

class _ColorFilter(logging.Filter):
	def filter(self, record):
		record.color = _colors.get(record.levelno, '')
		if record.levelno > logging.DEBUG:
			record.bold = BOLD
		else:
			record.bold = ''
		return True

_color_filter = _ColorFilter()

# add a trace level
TRACE_LVL = 5
def _trace(_self, message, *args, **kws):
	_self.log(TRACE_LVL, message, *args, **kws)
logging.addLevelName(TRACE_LVL, "TRACE")
logging.Logger.trace = _trace

def getLogger(*a):
	logger = logging.getLogger(*a)
	logger.addFilter(_color_filter)
	return logger


