param(
    [Parameter(Mandatory = $false)]
    [string]$RepositoryPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path,

    [Parameter(Mandatory = $false)]
    [string]$TaskName = "Academic CFP Tracker Weekly",

    [Parameter(Mandatory = $false)]
    [string]$At = "08:00"
)

$ErrorActionPreference = "Stop"
$Repo = Resolve-Path -LiteralPath $RepositoryPath
$Runner = Join-Path $Repo "scripts\run_weekly.ps1"

if (!(Test-Path -LiteralPath $Runner)) {
    throw "Cannot find runner script: $Runner"
}

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $At
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Weekly JCR Q1 CFP tracker report generator and git updater." -Force
Write-Host "Registered scheduled task '$TaskName' for every Monday at $At."

