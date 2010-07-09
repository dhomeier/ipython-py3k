""" Install various IPython completers

IPython extension that installs completers related to core ipython behaviour.

The actual implementations are in extensions/ipy_completers.py

"""
from IPython.core import ipapi

ip = ipapi.get()

from .ipy_completers import *

ip.set_hook('complete_command', module_completer, str_key = 'import')
ip.set_hook('complete_command', module_completer, str_key = 'from')
ip.set_hook('complete_command', runlistpy, str_key = '%run')
ip.set_hook('complete_command', cd_completer, str_key = '%cd')
