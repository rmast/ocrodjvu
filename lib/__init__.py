from __future__ import unicode_literals
import sys

if sys.version_info < (3, 6):  # no coverage
    raise RuntimeError('Python >= 3.6 is required')
if sys.version_info < (3, 0):  # no coverage
    raise RuntimeError('Python 3.X is required')

# vim:ts=4 sts=4 sw=4 et
