# protect-env.ps1
# Blocks Read, Grep, and Bash tool calls that target .env secret files.
# Claude Code PreToolUse hook — exit 2 = blocked, exit 0 = allowed.

$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json -ErrorAction SilentlyContinue
if (-not $payload) { exit 0 }

$tool = $payload.tool_name
$ti   = $payload.tool_input

function Test-EnvFile([string]$p) {
    # Matches: .env  .env.local  .env.production  .env.example is allowed through
    $name = [System.IO.Path]::GetFileName($p)
    return ($name -match '^\.env$') -or ($name -match '^\.env\.[^e]')
    # .env.example ends in 'e' but we still want to allow it, so check explicitly
}

function Test-EnvFileStrict([string]$p) {
    # Blocks only the real .env (not .env.example which is safe to read)
    $name = [System.IO.Path]::GetFileName($p)
    return $name -eq '.env'
}

switch ($tool) {
    "Read" {
        if (Test-EnvFileStrict $ti.file_path) {
            Write-Output "BLOCKED: .env contains secrets and cannot be read by Claude. Use .env.example for reference."
            exit 2
        }
    }
    "Grep" {
        if (Test-EnvFileStrict $ti.path) {
            Write-Output "BLOCKED: .env cannot be searched — it contains secrets."
            exit 2
        }
        # Also block if glob pattern would include .env
        if ($ti.glob -and $ti.glob -match '\.env$') {
            Write-Output "BLOCKED: glob pattern targets .env secret file."
            exit 2
        }
    }
    "Bash" {
        $cmd = $ti.command
        # Block any command that reads the .env file directly
        # Matches: cat .env  |  type .env  |  grep ... .env  |  Get-Content .env  etc.
        if ($cmd -match '\b(cat|type|head|tail|less|more|bat)\s+[''"]?\.env[''"]?\b' -or
            $cmd -match 'Get-Content\s+[''"]?\.env[''"]?' -or
            $cmd -match '\b(grep|rg|ripgrep|findstr|Select-String)\b.*[''"]?\.env[''"]?' -or
            $cmd -match '<\s*[''"]?\.env[''"]?'
        ) {
            Write-Output "BLOCKED: .env file read denied. Secrets stay in environment — use os.getenv() in code."
            exit 2
        }
    }
}

exit 0
