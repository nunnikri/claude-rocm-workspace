1# Run this script as Administrator in PowerShell
# Sets up two scheduled tasks for automated PR review and issue analysis

$WorkspaceDir = "C:\Project\Claude-Projects\claude-rocm-workspace"
$Script = "$WorkspaceDir\scripts\auto_review\poll_and_review.py"
$LogDir = "$WorkspaceDir\scripts\auto_review\logs"
$PythonExe = "C:\Users\nunnikri\AppData\Local\Microsoft\WindowsApps\python.exe"

# Create logs directory
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# --- Task 1: 10 PM PST (22:00) ---
$Action1 = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $Script `
    -WorkingDirectory $WorkspaceDir

$Trigger1 = New-ScheduledTaskTrigger -Daily -At "10:00 PM"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

Register-ScheduledTask `
    -TaskName "ROCm-AutoReview-10PM" `
    -Action $Action1 `
    -Trigger $Trigger1 `
    -Settings $Settings `
    -Description "Automated ROCm PR review and issue analysis - 10 PM run" `
    -RunLevel Highest `
    -Force

Write-Host "✅ Task 'ROCm-AutoReview-10PM' created (runs at 10:00 PM daily)"

# --- Task 2: 7 AM PST ---
$Action2 = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $Script `
    -WorkingDirectory $WorkspaceDir

$Trigger2 = New-ScheduledTaskTrigger -Daily -At "7:00 AM"

Register-ScheduledTask `
    -TaskName "ROCm-AutoReview-7AM" `
    -Action $Action2 `
    -Trigger $Trigger2 `
    -Settings $Settings `
    -Description "Automated ROCm PR review and issue analysis - 7 AM run" `
    -RunLevel Highest `
    -Force

Write-Host "✅ Task 'ROCm-AutoReview-7AM' created (runs at 7:00 AM daily)"
Write-Host ""
Write-Host "To verify: Get-ScheduledTask | Where-Object {`$_.TaskName -like 'ROCm-AutoReview*'}"
Write-Host "To run now: Start-ScheduledTask -TaskName 'ROCm-AutoReview-10PM'"
Write-Host "To remove:  Unregister-ScheduledTask -TaskName 'ROCm-AutoReview-10PM' -Confirm:`$false"
