# Installing the UI service on Windows Server

This guide installs the team web UI (`pd-ui`) on a City Windows Server VM, the way ADRs 0005 and
0008 describe:

- IIS terminates TLS with a City PKI certificate and proxies to the service;
- the service (uvicorn) listens on `127.0.0.1` only, as a Windows Service under a dedicated
  account with no local admin rights;
- secrets come from the City's enterprise vault at startup and stay in memory.

It covers issue #71. **Status: a draft, not yet rehearsed on a City server.** Each section
ends with a check. Correct this guide wherever the real server differs, so that by
production it is the tested runbook.

Install on DEV first, then TEST, then production.

**Phase 1 is read only (ADR 0015), and the credentials enforce it, not the code.** The code
on `main` already includes Apply (Phase 2) and Approvals (Phase 3), and has no switch to
turn them off. Until the City security review has passed, give the service a **read-only**
PagerDuty key and no ServiceNow write access. Then dry runs and reports work, and any apply
fails at PagerDuty or ServiceNow with an auth error (exit 3) before anything is written.

## Contents

1. [Before you start: requests to file](#1-before-you-start-requests-to-file)
2. [Server layout](#2-server-layout)
3. [Build the release on a workstation](#3-build-the-release-on-a-workstation)
4. [Prepare the server](#4-prepare-the-server)
5. [Database](#5-database)
6. [Install the release](#6-install-the-release)
7. [Secrets and the start script](#7-secrets-and-the-start-script)
8. [Register the Windows Service](#8-register-the-windows-service)
9. [IIS reverse proxy and TLS](#9-iis-reverse-proxy-and-tls)
10. [Firewall](#10-firewall)
11. [Verify the installation](#11-verify-the-installation)
12. [Upgrade and roll back](#12-upgrade-and-roll-back)
13. [Troubleshooting](#13-troubleshooting)
14. [Appendix: environment variables](#appendix-environment-variables)

Run all commands on the server in an elevated PowerShell unless a step says otherwise.
Paths and names in `<angle brackets>` are placeholders. The other names (`C:\pd-ops`,
`svc-pdops`, `PDOps`) are suggestions; if you change one, change it everywhere.

---

## 1. Before you start: requests to file

Most of the lead time is other teams. File these requests early. They don't depend on each
other.

| Team | Request | You need back |
|---|---|---|
| Windows server team | A Windows Server VM for each of DEV, TEST and PROD. 2 vCPU / 8 GB RAM / 100 GB data disk is a reasonable start. Local admin for the installer during setup. | Host names; whether a `D:` data disk exists |
| Active Directory | A **group managed service account (gMSA)**, e.g. `CITY\svc-pdops$`, usable by each VM. If gMSA isn't allowed, a domain service account with a vaulted password. Not a member of any admin group. | Account name |
| AD / Okta admins | AD groups `PD-Ops-Viewer`, `PD-Ops-Operator` and `PD-Ops-Approver`, pushed to Okta. The whole admin team starts in `PD-Ops-Approver` (ADR 0008). | Confirmation they show in Okta |
| Okta admins | An **OIDC Web App** for sign-in, described [below](#okta-sign-in-app). This is separate from the `pd-sn-sync` client-credentials app in [servicenow-okta-setup.md](servicenow-okta-setup.md). | Issuer URL, client ID; the client secret goes into the vault |
| DNS | An internal record, e.g. `pd-ops-dev.dallas.gov`, pointing at the VM | The host name |
| PKI | A server certificate for that host name, as a `.pfx` with its private key, from the City CA | `.pfx` and its password, through the vault or a secure share |
| DBA | A SQL Server database (`PDOps`) on a City-managed instance, with the logins described in [section 5](#5-database), and backups under the DBAs' normal retention | Server/instance name, database name |
| Network / firewall | **Inbound** TCP 443 to the VM from City networks only. **Outbound** direct HTTPS (TCP 443, no proxy) from the VM to `api.pagerduty.com`, `dallastx.service-now.com`, `dallastx-test.service-now.com`, `dallastx-dev.service-now.com` and the City's Okta domain (ADR 0008). Plus whatever the vault needs. | Change ticket numbers |
| Vault team | Secret entries for the values in [section 7](#7-secrets-and-the-start-script), readable **only** by the service account, and the supported way for a Windows service to read them (CLI, REST API with Windows auth, Credential Provider, managed identity, …) | The retrieval method and secret paths |
| PagerDuty admins | A scoped account API key for the service (ADR 0006). For Phase 1 it **must be read-only**: that key is what keeps Phase 1 read only (see the note at the top). | Stored in the vault |
| Security | Book the City security review before Phase 1 goes live (ADR 0015) | Date |

Note the software you will copy to the server. It probably can't reach the internet, because
outbound access is limited to the hosts above:

- Python 3.12 for Windows (x64) installer, from python.org
- Microsoft ODBC Driver 18 for SQL Server (x64)
- IIS **URL Rewrite 2.1** and **Application Request Routing (ARR) 3.0** installers
- WinSW **2.12.0**, `WinSW-x64.exe`, from github.com/winsw/winsw/releases
- The release bundle you build in [section 3](#3-build-the-release-on-a-workstation)

### Okta sign-in app

Ask the Okta admins for:

- **Type:** OIDC, Web Application, grant type *Authorization Code*. The service uses PKCE
  with a client secret.
- **Sign-in redirect URI:** `https://<host>/auth/callback`
- **Sign-out redirect URI:** `https://<host>/`
- **Groups claim:** an ID token claim named `groups`, filtered to *starts with* `PD-Ops-`.
  It must be in the **ID token**, from the authorization server whose issuer you give the
  service. With a custom authorization server, the claim goes on that server, not only on
  the app.
- **Assignments:** the three `PD-Ops-*` groups.

Someone who signs in without one of the three groups gets a "not authorized" message.
Someone in several groups gets the highest role.

---

## 2. Server layout

```
C:\pd-ops\
  releases\
    1.0.0\              one folder per release: venv\, static\ (the built front end)
    1.1.0\
  current\              a junction to the release that runs
  service\
    pd-ui.exe           WinSW, renamed
    pd-ui.xml           WinSW configuration (no secrets)
    start-pd-ui.ps1     fetches secrets, then starts the service
    logs\
D:\pd-ops-data\
  runs\                 PD_UI_RUN_ROOT: each Run's input and output files (ADR 0007)
```

Put the Run files on the data disk, not `C:`. They are kept for 90 days (ADR 0013). If there
is no `D:`, use `C:\pd-ops-data`.

---

## 3. Build the release on a workstation

Build the release on a City Windows workstation with internet access, using Python 3.12 (the
version that will run on the server) and Node.js 20 or later. That way the server never needs
PyPI, npm or Node.

```powershell
git clone https://github.com/AJHMH/PagerDuty-Ops-Scripts.git
cd PagerDuty-Ops-Scripts
git checkout <release tag or commit>
```

Run the tests, then build every Python dependency as a Windows wheel:

```powershell
py -3.12 -m pip install -e ".[dev,ui]"
py -3.12 -m pytest
py -3.12 -m pip wheel ".[ui]" -w bundle\wheelhouse
```

Build the front end:

```powershell
npm --prefix ui ci
npm --prefix ui run build
Copy-Item ui\dist bundle\static -Recurse
```

Record what's in it, then zip it:

```powershell
git rev-parse HEAD > bundle\COMMIT.txt
Compress-Archive bundle\* pd-ops-<version>.zip
```

Copy `pd-ops-<version>.zip` to the server by the City's approved method.

---

## 4. Prepare the server

### 4.1 Software

1. Install **Python 3.12** for all users. Leave "Add to PATH" unchecked; the service uses
   full paths. The default location is `C:\Program Files\Python312\`.
2. Install **ODBC Driver 18 for SQL Server**.
3. Install IIS and its management tools:

   ```powershell
   Install-WindowsFeature Web-Server, Web-Mgmt-Console, Web-Http-Logging, Web-Request-Monitor
   ```

4. Install **URL Rewrite 2.1**, then **ARR 3.0**.

### 4.2 Folders and permissions

The service account reads the release and service folders, and writes only to the Run
folder and its own logs.

```powershell
$svc = 'CITY\svc-pdops$'   # gMSA: note the trailing $
New-Item -ItemType Directory C:\pd-ops\releases, C:\pd-ops\service\logs, D:\pd-ops-data\runs -Force

icacls C:\pd-ops /inheritance:r /grant 'Administrators:(OI)(CI)F' 'SYSTEM:(OI)(CI)F' "${svc}:(OI)(CI)RX"
icacls C:\pd-ops\service\logs /grant "${svc}:(OI)(CI)M"
icacls D:\pd-ops-data /inheritance:r /grant 'Administrators:(OI)(CI)F' 'SYSTEM:(OI)(CI)F' "${svc}:(OI)(CI)M"
```

Admin team members who need to read Run files on disk should get read access to
`D:\pd-ops-data\runs` through an AD group, not individually. Normally they download Run
files from the UI instead.

### 4.3 The service account

- **gMSA:** the server team runs `Install-ADServiceAccount svc-pdops` on the VM, and
  `Test-ADServiceAccount svc-pdops` returns `True`.
- **Domain account:** grant it **Log on as a service** (Local Security Policy → User Rights
  Assignment), or let the Services console grant it in [section 8](#8-register-the-windows-service).

Either way it is a member of no local or domain admin group.

**Check:**

```powershell
net localgroup Administrators
```

The account must not be listed.

---

## 5. Database

Ask the DBA for two kinds of access to `PDOps` (ADR 0007, #70):

| Who | Permissions | Used by |
|---|---|---|
| `CITY\svc-pdops$` (Windows login) | `db_datareader`, `db_datawriter` | the running service |
| A DBA or deploy account | `db_owner`, or `db_ddladmin` plus reader/writer | `pd-ui-migrate` on each deploy |

With Windows authentication there is no database password. The connection URL is not a
secret:

```
mssql+pyodbc://@<sql-server>[\<instance>]/PDOps?driver=ODBC+Driver+18+for+SQL+Server&Trusted_Connection=yes&Encrypt=yes
```

Driver 18 encrypts by default and checks the SQL Server's certificate. If the instance uses a
certificate the server doesn't trust, ask the DBA to fix the certificate rather than adding
`TrustServerCertificate=yes`.

You create the schema in [section 6](#6-install-the-release).

---

## 6. Install the release

Unzip the bundle and create the virtual environment. This uses only the bundled wheels:

```powershell
$ver = '<version>'
$rel = "C:\pd-ops\releases\$ver"
Expand-Archive "<path>\pd-ops-$ver.zip" $rel
& 'C:\Program Files\Python312\python.exe' -m venv "$rel\venv"
& "$rel\venv\Scripts\python.exe" -m pip install --no-index --find-links "$rel\wheelhouse" "pagerduty-ops-scripts[ui]"
```

Point `current` at the release:

```powershell
cmd /c mklink /J C:\pd-ops\current $rel
```

Create the schema, **as the DBA or deploy account**, in a PowerShell session run as that
account:

```powershell
$env:PD_UI_DATABASE_URL = '<connection URL from section 5>'
& C:\pd-ops\current\venv\Scripts\pd-ui-migrate.exe
```

It prints `Schema at <revision> (was empty).` Running it again is safe. Exit code 3 means
the login was refused; 2 means it couldn't connect.

**Check:** `& C:\pd-ops\current\venv\Scripts\pd-list-teams.exe --help` prints help.

---

## 7. Secrets and the start script

### 7.1 What goes in the vault

| Environment variable | Value | Notes |
|---|---|---|
| `PD_UI_SESSION_SECRET` | A random key that signs session cookies | Generate once per environment: `py -c "import secrets; print(secrets.token_urlsafe(48))"`, paste it straight into the vault, then clear the screen. Changing it signs everyone out. |
| `PD_UI_OKTA_CLIENT_SECRET` | The sign-in app's client secret | Used by the service only; never passed to Runs |
| `PD_API_TOKEN` | The PagerDuty account API key (ADR 0006) | Must be a read-only key for Phase 1; replace it with a full-access key only after the security review approves writes |
| `OKTA_CLIENT_SECRET` | The `pd-sn-sync` client-credentials app secret | Only if ServiceNow commands are used |
| `SN_CLIENT_SECRET` / `SN_PASSWORD` | Only for the fallback `SN_AUTH_METHOD` values | Usually not needed |

The vault grants read access to these entries to the service account only.

### 7.2 Why a start script, not service environment settings

WinSW `<env>` entries are written to `pd-ui.xml`, and NSSM's environment settings to the
registry. Both are **on disk**, which ADR 0008 forbids for secrets. So the service runs a
start script. The script reads the secrets from the vault into its own process environment,
in memory, and then starts `pd-ui` as its child, which inherits them. Non-secret settings go
in `pd-ui.xml`.

### 7.3 `C:\pd-ops\service\start-pd-ui.ps1`

Replace the body of `Get-VaultSecret` with the vault team's supported method. Whatever it
uses, it must authenticate **as the service account** (Windows authentication, a gMSA or a
managed identity), not with a password stored on the server. Otherwise the vault is just a
second secret on disk.

```powershell
# Starts pd-ui with its secrets from the City vault. The secrets live only in this
# process's environment and pd-ui's (ADR 0008). Never echo or log them, and never use
# Start-Transcript here.
$ErrorActionPreference = 'Stop'

function Get-VaultSecret([string] $Path) {
    # TODO: replace with the City vault's supported retrieval, authenticating as this
    # service account. It must return the secret as a string and throw on failure.
    throw "Get-VaultSecret is not configured (wanted $Path)."
}

# Environment variable -> vault path. Remove the lines you don't use.
$secrets = [ordered]@{
    PD_UI_SESSION_SECRET     = '<vault path>/pd-ops/<env>/session-secret'
    PD_UI_OKTA_CLIENT_SECRET = '<vault path>/pd-ops/<env>/okta-signin-client-secret'
    PD_API_TOKEN             = '<vault path>/pd-ops/<env>/pagerduty-api-key'
    OKTA_CLIENT_SECRET       = '<vault path>/pd-ops/<env>/okta-sn-sync-client-secret'
}

foreach ($name in $secrets.Keys) {
    try {
        $value = Get-VaultSecret $secrets[$name]
    } catch {
        # Name the variable and path, never the value.
        Write-Error "Could not read $name from the vault ($($secrets[$name])): $($_.Exception.Message)"
        exit 2
    }
    if ([string]::IsNullOrWhiteSpace($value)) {
        Write-Error "$name came back empty from the vault ($($secrets[$name]))."
        exit 2
    }
    [Environment]::SetEnvironmentVariable($name, $value, 'Process')
}
Remove-Variable value

& 'C:\pd-ops\current\venv\Scripts\pd-ui.exe'
exit $LASTEXITCODE
```

Lock the script down so only administrators can change it. The service account can read and
run it but can't change what it does:

```powershell
icacls C:\pd-ops\service\start-pd-ui.ps1 /inheritance:r /grant 'Administrators:F' 'SYSTEM:F' 'CITY\svc-pdops$:RX'
```

If the City requires signed PowerShell scripts, sign it, and replace `-ExecutionPolicy Bypass`
in the next section with the policy the server team uses.

**Check:** don't test the script by running it as yourself. Your account should *not* be
able to read the service's secrets. You test it by starting the service in section 8.

---

## 8. Register the Windows Service

Copy `WinSW-x64.exe` to `C:\pd-ops\service\pd-ui.exe`, and create
`C:\pd-ops\service\pd-ui.xml`. It holds **no secrets**:

```xml
<service>
  <id>pd-ui</id>
  <name>PagerDuty Ops UI</name>
  <description>Team web UI over the PagerDuty Ops CLI (localhost only; IIS fronts it).</description>

  <executable>C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe</executable>
  <arguments>-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\pd-ops\service\start-pd-ui.ps1"</arguments>
  <workingdirectory>D:\pd-ops-data</workingdirectory>

  <!-- Non-secret configuration. Secrets come from the vault (start-pd-ui.ps1). -->
  <env name="PD_UI_DATABASE_URL" value="mssql+pyodbc://@SQLHOST/PDOps?driver=ODBC+Driver+18+for+SQL+Server&amp;Trusted_Connection=yes&amp;Encrypt=yes"/>
  <env name="PD_UI_RUN_ROOT" value="D:\pd-ops-data\runs"/>
  <env name="PD_UI_STATIC_DIR" value="C:\pd-ops\current\static"/>
  <env name="PD_UI_PORT" value="8080"/>
  <env name="PD_UI_OKTA_ISSUER" value="https://ORG.okta.com/oauth2/default"/>
  <env name="PD_UI_OKTA_CLIENT_ID" value="CLIENT_ID"/>
  <env name="PD_UI_OKTA_REDIRECT_URI" value="https://HOST/auth/callback"/>
  <env name="PD_FROM_EMAIL" value="pd-ops-service@dallas.gov"/>
  <env name="SN_INSTANCE" value="dallastx-dev"/>
  <env name="SN_PROD_INSTANCES" value="dallastx"/>
  <env name="SN_REHEARSAL_INSTANCES" value="dallastx-dev,dallastx-test"/>
  <env name="SN_AUTH_METHOD" value="okta"/>
  <env name="OKTA_DOMAIN" value="ORG.okta.com"/>
  <env name="OKTA_AUTH_SERVER_ID" value="AUTH_SERVER_ID"/>
  <env name="OKTA_CLIENT_ID" value="SN_SYNC_CLIENT_ID"/>
  <env name="OKTA_SCOPE" value="servicenow.table"/>

  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>10</keepFiles>
  </log>
  <logpath>C:\pd-ops\service\logs</logpath>

  <!-- Give a clean shutdown time to stop executing Runs and leave the queue (docs/ui.md). -->
  <stoptimeout>60 sec</stoptimeout>
  <onfailure action="restart" delay="30 sec"/>
  <onfailure action="restart" delay="2 min"/>
  <resetfailure>1 hour</resetfailure>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
</service>
```

Replace the capitalised placeholders. In XML, `&` in the URL must be written `&amp;`, as
shown. Set `PD_FROM_EMAIL` to the PagerDuty user email the account key acts as, if commands
need one. See the [appendix](#appendix-environment-variables) for what each variable does.

Install the service, then set its account. Don't put the account's password in the XML:

```powershell
C:\pd-ops\service\pd-ui.exe install
```

For a gMSA, set the account with no password:

```powershell
sc.exe config pd-ui obj= 'CITY\svc-pdops$' password= ''
```

For a domain account, set it in **services.msc** → *PagerDuty Ops UI* → *Log On*, and type
the password there.

Start it:

```powershell
Start-Service pd-ui
```

**Check:**

```powershell
Get-CimInstance Win32_Service -Filter "Name='pd-ui'" | Select-Object Name, State, StartName
Get-NetTCPConnection -LocalPort 8080 -State Listen | Select-Object LocalAddress, OwningProcess
```

- `State` is `Running` and `StartName` is the service account.
- The only listener is `127.0.0.1`, never `0.0.0.0` or `::`.
- `C:\pd-ops\service\logs\pd-ui.err.log` shows uvicorn running on `http://127.0.0.1:8080`.

If it stops straight away, see [Troubleshooting](#13-troubleshooting).

---

## 9. IIS reverse proxy and TLS

### 9.1 Certificate

Import the City PKI certificate into the machine store. The key is not exportable:

```powershell
$pfxPassword = Read-Host -AsSecureString 'PFX password'
Import-PfxCertificate -FilePath '<path>\pd-ops.pfx' -CertStoreLocation Cert:\LocalMachine\My -Password $pfxPassword
```

Then delete the `.pfx` from the server. Check that the City root and intermediate CAs are
trusted, under `Cert:\LocalMachine\Root` and `CA`. TLS versions and ciphers follow the
server team's hardening baseline. TLS 1.0 and 1.1 should be off.

### 9.2 Enable the ARR proxy

```powershell
$appHost = 'MACHINE/WEBROOT/APPHOST'
Set-WebConfigurationProperty -PSPath $appHost -Filter system.webServer/proxy -Name enabled -Value True
# Keep the public Host header, and don't let ARR rewrite redirect Location headers
# (that breaks the Okta sign-in redirects).
Set-WebConfigurationProperty -PSPath $appHost -Filter system.webServer/proxy -Name preserveHostHeader -Value True
Set-WebConfigurationProperty -PSPath $appHost -Filter system.webServer/proxy -Name reverseRewriteHostInResponseHeaders -Value False
# Let the site's rewrite rule set X-Forwarded-Proto.
Add-WebConfigurationProperty -PSPath $appHost -Filter system.webServer/rewrite/allowedServerVariables -Name '.' -Value @{ name = 'HTTP_X_FORWARDED_PROTO' }
```

ARR adds `X-Forwarded-For` itself. The service trusts the forwarded headers only from
`127.0.0.1`, which is where IIS connects from (`pagerduty_ops_ui/serve.py`).

### 9.3 The site

Stop the Default Web Site, and create a site that serves no files of its own and listens on
HTTPS only:

```powershell
Stop-Website 'Default Web Site'
Set-ItemProperty 'IIS:\Sites\Default Web Site' serverAutoStart False
New-Item C:\inetpub\pd-ops -ItemType Directory -Force
New-WebAppPool pd-ops
Set-ItemProperty IIS:\AppPools\pd-ops managedRuntimeVersion ''
New-Website -Name pd-ops -PhysicalPath C:\inetpub\pd-ops -ApplicationPool pd-ops -Port 443 -Ssl -HostHeader '<host>' -SslFlags 1
$thumb = (Get-ChildItem Cert:\LocalMachine\My | Where-Object Subject -like '*<host>*').Thumbprint
(Get-WebBinding -Name pd-ops -Protocol https).AddSslCertificate($thumb, 'My')
```

Create `C:\inetpub\pd-ops\web.config`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="pd-ui" stopProcessing="true">
          <match url="(.*)" />
          <serverVariables>
            <set name="HTTP_X_FORWARDED_PROTO" value="https" />
          </serverVariables>
          <action type="Rewrite" url="http://127.0.0.1:8080/{R:1}" />
        </rule>
      </rules>
    </rewrite>
    <security>
      <requestFiltering>
        <!-- Match the service's 100 MB request cap (docs/ui.md). -->
        <requestLimits maxAllowedContentLength="104857600" />
      </requestFiltering>
    </security>
    <httpProtocol>
      <customHeaders>
        <remove name="X-Powered-By" />
        <add name="Strict-Transport-Security" value="max-age=31536000" />
      </customHeaders>
    </httpProtocol>
  </system.webServer>
</configuration>
```

Users reach the service at `https://<host>/` only. There is no HTTP binding, so plain-HTTP
requests are refused rather than redirected. Add a port 80 binding with a redirect rule only
if the City's standard calls for one. The service's session cookie is HTTPS-only, so sign-in
only works through this site, never directly against port 8080.

**Check** from your workstation: `https://<host>/` shows a valid certificate with no browser
warning, and takes you to Okta sign-in.

---

## 10. Firewall

On the VM, allow inbound HTTPS from City networks only. The network team's perimeter rules
are the main control; this is defence in depth:

```powershell
New-NetFirewallRule -DisplayName 'pd-ops HTTPS (City networks)' -Direction Inbound -Protocol TCP -LocalPort 443 -RemoteAddress <City CIDR ranges> -Action Allow
```

Port 8080 needs no rule, because it listens on localhost only.

**Check outbound access** (ADR 0008). It must be direct, with no proxy:

```powershell
netsh winhttp show proxy           # expect: Direct access (no proxy server).
Get-ChildItem Env: | Where-Object Name -match 'proxy'   # expect: nothing
'api.pagerduty.com','dallastx.service-now.com','dallastx-test.service-now.com',
'dallastx-dev.service-now.com','<org>.okta.com' | ForEach-Object {
    $r = Test-NetConnection $_ -Port 443 -WarningAction SilentlyContinue
    '{0,-34} {1}' -f $_, $(if ($r.TcpTestSucceeded) { 'OK' } else { 'BLOCKED' })
}
```

Every host must say `OK`. Also check that TLS works end to end with no TLS-inspecting proxy
in the way. Expect `401`, since no token is sent:

```powershell
try { Invoke-WebRequest https://api.pagerduty.com/users -UseBasicParsing } catch { $_.Exception.Response.StatusCode.value__ }
```

---

## 11. Verify the installation

Do all of these on DEV and TEST, and attach the results to the security review.

| # | Check | Expected |
|---|---|---|
| 1 | Service account (section 8) | Running as `svc-pdops`, not an admin |
| 2 | Listener (section 8) | `127.0.0.1:8080` only |
| 3 | From another City machine: `Test-NetConnection <host> -Port 8080` | Fails |
| 4 | From outside the City network (e.g. a phone off Wi-Fi), browse `https://<host>/` | Unreachable |
| 5 | Sign in as a member of each `PD-Ops-*` group; open `https://<host>/api/me` | Your name and the right role |
| 6 | Sign in as someone in none of the groups | "Not authorized" message naming the three groups |
| 7 | Run a read-only command, e.g. List teams | Succeeds; its output and files can be downloaded |
| 8 | As a Viewer, try to dry-run a writing command (e.g. Apply tags) | Refused (403); Operators and Approvers can dry-run it |
| 8a | Confirm the PagerDuty key in the vault is read-only, and the ServiceNow integration user has no write ACLs (Phase 1) | Confirmed by the PagerDuty and ServiceNow admins |
| 9 | Run history shows the Run after `Restart-Service pd-ui` | Still listed; anything that was executing shows Stopped |
| 10 | Stop the service while a long read-only Run executes, then `Get-Process python` | No Run processes left; the Run shows Stopped |
| 11 | Secrets aren't on disk (below) | No matches |

**Secrets not on disk (check 11).** Run this as an administrator who may read the vault
entry, after a few Runs. The secret goes into a variable only, so it doesn't end up on the
command line or in PowerShell history:

```powershell
$s = <vault retrieval of the PagerDuty key, into a variable>
$hits = Get-ChildItem C:\pd-ops, D:\pd-ops-data, C:\inetpub\logs -Recurse -File -ErrorAction SilentlyContinue |
    Select-String -SimpleMatch $s -List | Select-Object -ExpandProperty Path
$reg = Get-ChildItem HKLM:\SYSTEM\CurrentControlSet\Services\pd-ui -Recurse | Get-ItemProperty |
    Out-String | Select-String -SimpleMatch $s
"Files: $($hits.Count)  Registry: $(@($reg).Count)"
Remove-Variable s
```

Both counts must be 0. Also ask the DBA to confirm no column in `PDOps` contains it: Runs
store `[redacted]` in its place.

---

## 12. Upgrade and roll back

Upgrades go DEV → TEST → PROD, in a change window. Users are signed out, and anything that
is executing is stopped.

1. Build the new bundle ([section 3](#3-build-the-release-on-a-workstation)) and install it
   into a **new** folder, `C:\pd-ops\releases\<new>`. Do everything in
   [section 6](#6-install-the-release) except the junction and the migration.
2. Ask the DBA for a backup of `PDOps`, and note its time.
3. Stop the service: `Stop-Service pd-ui`. This waits for executing Runs to stop; queued
   applies stay queued.
4. As the DBA or deploy account, migrate with the **new** release:
   `& C:\pd-ops\releases\<new>\venv\Scripts\pd-ui-migrate.exe`.
5. Switch the junction:

   ```powershell
   cmd /c rmdir C:\pd-ops\current
   cmd /c mklink /J C:\pd-ops\current C:\pd-ops\releases\<new>
   ```

6. Start the service with `Start-Service pd-ui`, and repeat checks 2, 5, 7 and 9 from
   section 11.
7. Keep the previous release folder until the next upgrade, then delete it.

**Roll back.**

- If step 4 changed nothing ("Schema at X (was X)"): stop the service, point the junction
  back at the old release, and start it.
- If the migration did change the schema: the old release refuses to start on the new
  schema, by design. Stop the service, have the DBA restore the backup from step 2, point
  the junction back, and start it. Runs recorded since the backup are lost, so roll back
  before reopening the service, or accept that loss.

**Rotate a secret.** Update it in the vault, then `Restart-Service pd-ui`. The service only
reads the vault at startup. If you rotate `PD_UI_SESSION_SECRET`, everyone is signed out.

---

## 13. Troubleshooting

Logs: `C:\pd-ops\service\logs\pd-ui.out.log` / `pd-ui.err.log` / `pd-ui.wrapper.log`, and the
IIS logs in `C:\inetpub\logs\LogFiles`.

| Symptom | Likely cause | Action |
|---|---|---|
| Service stops at once; log says `X is not set.` | A required variable is missing from `pd-ui.xml` or the vault | Add it (see the [appendix](#appendix-environment-variables)) |
| Log says `Could not read … from the vault` | The vault denied the service account, or `Get-VaultSecret` isn't configured | Check the vault entry's access policy names the service account |
| Log says `Run pd-ui-migrate first.` | The schema is behind this release | Run [section 6](#6-install-the-release)'s migration with the deploy account |
| Log shows a SQL `Login failed` | The service account has no SQL login or role | DBA grants `db_datareader` / `db_datawriter` |
| PowerShell error about execution policy | Script signing required | Sign the script, or use the server team's policy |
| IIS returns 502.3 | `pd-ui` isn't running, or isn't on the port in `web.config` | `Get-Service pd-ui`; check `PD_UI_PORT` |
| IIS returns 404 for everything | The ARR proxy isn't enabled, or URL Rewrite isn't installed | Repeat [section 9.2](#92-enable-the-arr-proxy) |
| IIS returns 413 on upload | Request above `maxAllowedContentLength` | Expected above 100 MB |
| Sign-in loops back to Okta, or the cookie is never set | Browsing over plain HTTP or straight to 8080; or `X-Forwarded-Proto` isn't set | Use `https://<host>/`; check the `serverVariables` rule and the allowed server variable |
| Okta error `redirect_uri` mismatch | `PD_UI_OKTA_REDIRECT_URI` differs from the URI registered in Okta | Make them identical, including the scheme and host |
| Signed in but "not authorized" | No `groups` claim in the ID token, or the user is in none of the groups | Okta admin checks the claim and filter ([section 1](#okta-sign-in-app)) |
| Runs fail with exit 3 | The PagerDuty or ServiceNow credential was rejected | Check the vault value; rotate it if needed |
| Runs to ServiceNow fail with timeouts | Outbound access blocked | Repeat the [section 10](#10-firewall) checks |

---

## Appendix: environment variables

All of these are read at startup (`pagerduty_ops_ui/settings.py`). **Secrets** come from the
vault through the start script; everything else is set in `pd-ui.xml`.

**Used by the service itself:**

| Variable | Required | Secret | Meaning |
|---|---|---|---|
| `PD_UI_DATABASE_URL` | yes | no (with Windows auth) | SQL Server connection URL ([section 5](#5-database)) |
| `PD_UI_RUN_ROOT` | yes | no | Folder for each Run's files |
| `PD_UI_SESSION_SECRET` | yes | **yes** | Signs the session cookie |
| `PD_UI_OKTA_ISSUER` | yes | no | Okta authorization server issuer URL |
| `PD_UI_OKTA_CLIENT_ID` | yes | no | Sign-in app client ID |
| `PD_UI_OKTA_CLIENT_SECRET` | yes | **yes** | Sign-in app client secret |
| `PD_UI_OKTA_REDIRECT_URI` | yes | no | `https://<host>/auth/callback` |
| `PD_UI_STATIC_DIR` | yes in practice | no | `C:\pd-ops\current\static` |
| `PD_UI_PORT` | no | no | Port on 127.0.0.1; default 8080 |

**Passed through to Runs.** Only these, plus a short allowlist (`PATH`, `TEMP`, …), reach a
Run's process:

| Variable | Secret | Meaning |
|---|---|---|
| `PD_API_TOKEN` | **yes** | PagerDuty account API key |
| `OKTA_CLIENT_ID`, `OKTA_CLIENT_SECRET` | secret: **yes** | `pd-sn-sync`'s Okta client-credentials app |
| `SN_CLIENT_ID`, `SN_CLIENT_SECRET`, `SN_USER`, `SN_PASSWORD` | **yes** | Only for `SN_AUTH_METHOD=sn-oauth` / `basic` |
| `PD_FROM_EMAIL` | no | `From` header for commands that need one |
| `SN_INSTANCE`, `SN_PROD_INSTANCES`, `SN_REHEARSAL_INSTANCES`, `SN_FIELD_MAP`, `SN_AUTH_METHOD` | no | ServiceNow settings ([servicenow.md](servicenow.md)) |
| `OKTA_DOMAIN`, `OKTA_AUTH_SERVER_ID`, `OKTA_SCOPE` | no | `pd-sn-sync`'s Okta authorization server and scope ([servicenow-okta-setup.md](servicenow-okta-setup.md)) |
