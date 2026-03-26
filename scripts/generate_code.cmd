@echo off
REM Generate Python code from the Filament backend.
REM Usage: scripts\generate_code.cmd <backend_repo> [-o <output_path>]

python "%~dp0generate_code.py" %*
