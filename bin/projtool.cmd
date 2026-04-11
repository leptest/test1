@echo off
rem Windows wrapper for projtool. Forwards to python.
python "%~dp0..\projtool.py" %*
