@echo off
cd /d C:\Users\Administrator\Desktop\projects\gojep
venv\Scripts\python.exe -m cli.runner process-email-updates >> logs\run_emails.log 2>&1
