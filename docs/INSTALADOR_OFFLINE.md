**English** | [Português (Brasil)](INSTALADOR_OFFLINE.pt-BR.md)

# Offline installer for Windows

## End-user outcome

`Setup-BulkFlow.exe` is the recommended package for a new Windows x64
workstation or one without the required SQL clients. It combines the following
components in a single file:

- `BulkFlowGUI.exe` and `BulkFlowCLI.exe`, with Python, Tk, and the project's
  Python libraries already included;
- Microsoft ODBC Driver 18 for SQL Server;
- Microsoft Command Line Utilities, which provides the `bcp` utility.

The installer works without internet access. The Microsoft components are
embedded as official installers and installed in Windows; they are not copied
into the application executables or modified by the project.

After installation, the operator does not need to install Python, run `pip`, or
locate BCP manually. The ODBC Driver and BCP remain native system components,
now provisioned by the offline package itself.

The two standalone EXEs remain available for portable distribution. In that
mode, an administrator must still install the ODBC Driver and BCP separately.

## Components pinned in this release

| Component | Offline package version |
|---|---:|
| BulkFlow | 2.0.0 |
| Microsoft ODBC Driver 18 for SQL Server (x64) | 18.7.1.1 |
| Microsoft Command Line Utilities/BCP (x64) | 17.0.4055.5 |

BCP build 17 included in this release was selected because its help output
confirms support for the `-Y` and `-u` TLS security controls required by the
engine. The installer does not download newer versions while it runs. Updating
a component requires building and qualifying a new release.

Official sources:

- [Download Microsoft ODBC Driver for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server?view=sql-server-ver17);
- [ODBC Driver requirements, installation, license, and `APPGUID` property](https://learn.microsoft.com/en-us/sql/connect/odbc/windows/system-requirements-installation-and-driver-files?view=sql-server-ver17);
- [Download and install the BCP utility](https://learn.microsoft.com/en-us/sql/tools/bcp/bcp-download-install?view=sql-server-ver17).

## Interactive installation

1. Copy `Setup-BulkFlow.exe` to the Windows x64 workstation through a
   controlled channel.
2. Verify the SHA-256 hash published with the release.
3. Run the installer and approve the User Account Control (UAC) prompt.
   Elevation is required to install under `Program Files` and register the
   Microsoft components for the machine.
4. Read and accept the license terms displayed by the installer.
5. Select **Install** and wait for confirmation that installation is complete.
6. Open the interface through the **BulkFlow** shortcut, or run
   `BulkFlowCLI.exe` in a terminal.
7. Before the first planning operation, select **Check prerequisites** in the
   GUI or run the CLI `prerequisites` command.

The installer does not request or store database passwords. Source,
`BD_DESTINO_01`, and `BD_DESTINO_02` credentials continue to be requested at operation time, with an
independent masked password for each connection.

## Silent installation

Open PowerShell or Command Prompt with the appropriate elevation policy and
run:

```powershell
New-Item -ItemType Directory -Force C:\Logs\BulkFlow | Out-Null
& .\Setup-BulkFlow.exe /install /quiet /norestart `
    /log "C:\Logs\BulkFlow\instalacao.log"
$exitCode = $LASTEXITCODE
```

Interpret installer and MSI package return codes as follows:

- `0`: completed without a restart request;
- `3010`: completed; a restart is required;
- `1641`: completed; the installer initiated a restart;
- any other value: failure; review the specified log.

For silent deployment, the organization distributing the package is
responsible for reviewing and accepting the Microsoft component license terms
in advance. Do not use silent mode as a way to bypass that approval.

The installer is fully offline: lack of network connectivity does not change
its contents or cause it to search for external packages.

## Directories and retained data

Immutable application files are installed by default under:

```text
C:\Program Files\BulkFlow\
```

Mutable user files remain outside `Program Files`, by default under:

```text
%LOCALAPPDATA%\BulkFlow\
├── bcp-data\
├── .bcp-control\
├── ddl\
└── config\
```

This layout applies to the installed application or to a standalone frozen
copy. When the GUI runs directly from source—or when the portable EXE is run
from that source tree's `release` directory—the three equivalent operational
directories are under `<project root>\Local\BulkFlow`. This prevents an
installed executable from attempting to write under `Program Files`.

This separation allows the application to run without write access to its
program directory. Configuration may still point operational directories to
another authorized location, including the share required by the active data
destination SQL Server.

Repair, upgrade, and uninstall operations preserve operational artifacts under
`%LOCALAPPDATA%\BulkFlow`, including configurations, manifests, BCP files, and
checkpoints. Uninstall removes the application, but does not automatically
remove the ODBC Driver and Command Line Utilities shared with other programs.
To delete working data, review and remove it separately only after confirming
that no resumable execution remains.

## Repair, upgrade, and uninstall

- **Repair:** run the same `Setup-BulkFlow.exe` again, or use **Installed
  apps** in Windows. Repair restores application files without deleting user
  data.
- **Upgrade:** run the installer from the new release. The package applies its
  version rules and must not silently replace a newer version with an older
  one.
- **Uninstall:** use **Settings > Apps > Installed apps**. Shared Microsoft
  clients and operational data are preserved as described above.

Explicitly qualify all three paths before distributing an upgrade. Do not
treat manually overwriting the EXEs as an upgrade of the installed product.

## Logs and diagnostics

For an attended installation, the bootstrapper creates its logs in the Windows
temporary directory. For support and automation, prefer specifying `/log` and
a protected, persistent path, as shown in the silent installation example.

A minimum diagnostic record should include:

```powershell
Get-OdbcDriver | Where-Object Name -eq 'ODBC Driver 18 for SQL Server'
Get-Command bcp -All
bcp -v
bcp -?
& "$env:ProgramFiles\BulkFlow\BulkFlowCLI.exe" prerequisites `
    --config .\config.v2.json
```

The installer records the provisioned BCP path in the system. The engine
prefers that known path when configuration specifies only `bcp`; an explicitly
configured absolute path still takes precedence.

## Security and authenticity

- Distribute the setup and its checksum file through the corporate repository
  or another authenticated channel.
- Verify SHA-256 before execution. A hash confirms integrity only when the
  reference value came from a trusted channel.
- Embedded Microsoft MSIs must retain a valid Authenticode signature from
  Microsoft Corporation. The build routine rejects packages whose hash differs
  from the pinned value.
- Do not include passwords, configuration files containing secrets, private
  certificates, or BCP artifacts in the installer.
- Restrict operational directories according to the model described in
  [Prerequisites](PRE_REQUISITOS.md); installation does not grant database
  permissions, open firewall ports, or create shares.

The artifact produced in this lab does not have a publisher code-signing
certificate. Windows may therefore show **Unknown publisher** in UAC or
Microsoft Defender SmartScreen. Do not conceal this condition: for production
distribution, sign the installer EXE, the application MSI, and the GUI/CLI EXEs
with a corporate code-signing certificate, then validate the signatures after
packaging. Until then, rely only on a hash published through a trusted channel
and limit distribution to qualification environments.

## Qualification checklist for a clean offline VM

Use a supported Windows x64 VM, with a pre-test snapshot and networking
disconnected. Record evidence for every item.

- [ ] Confirm that Python, ODBC Driver 18, and BCP are not installed.
- [ ] Validate the SHA-256 of `Setup-BulkFlow.exe` before execution.
- [ ] Install interactively, accept UAC/licenses, and confirm completion
      without a download or internet access.
- [ ] Confirm installation under `C:\Program Files\BulkFlow` and verify the
      shortcuts.
- [ ] Open the GUI and CLI with no Python on `PATH`.
- [ ] Confirm `ODBC Driver 18 for SQL Server` and run `bcp -v`/`bcp -?`.
- [ ] Run `prerequisites`, `plan`, DDL generation/application, and a test load
      with Source, `BD_DESTINO_01`, and `BD_DESTINO_02` configured; verify that
      at most one destination role includes data, and exactly one does so when
      the test imports data.
- [ ] Confirm that mutable files are created under `%LOCALAPPDATA%` or the
      configured paths, never under `Program Files`.
- [ ] Run setup again with the same versions and prove idempotency/repair.
- [ ] Test on a VM that already has the same Microsoft component versions.
- [ ] Test on a VM that already has newer compatible versions; no component
      may be downgraded.
- [ ] Corrupt a lab copy of a package and prove that integrity validation
      prevents delivery/installation.
- [ ] Test `/quiet /norestart /log`, capture the exit code, and review the
      logs.
- [ ] Simulate insufficient space and a package failure; confirm the message,
      nonzero exit code, and absence of misleading partial state.
- [ ] Run repair and confirm that configurations/checkpoints remain intact.
- [ ] Upgrade the application and prove data retention and downgrade blocking.
- [ ] Uninstall and confirm removal of the application while retaining user
      data and shared Microsoft clients.
- [ ] Validate an actual resume after reinstall/repair.
- [ ] Confirm that logs, the Windows registry, shortcuts, and installed
      directories contain no passwords.

Promote the package only after completing this procedure on a genuinely clean
VM. Administrative extraction, static MSI inspection, and tests on the build
workstation complement this qualification but do not replace it.
