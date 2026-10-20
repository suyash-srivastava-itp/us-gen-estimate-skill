"""What the finder scripts need to know about one Python interpreter.

find_python.sh and find_python.ps1 run this file with each candidate interpreter. It prints
three lines: the interpreter's full path, its major and minor version, and the numpy version
("missing" if numpy cannot be imported). It is a file rather than an inline command so the
same probe works from sh, PowerShell and cmd, where quoting differs.
"""
import sys

try:
    import numpy
    numpy_version = numpy.__version__
except Exception:
    numpy_version = "missing"

print(sys.executable)
print(sys.version_info[0], sys.version_info[1])
print(numpy_version)
