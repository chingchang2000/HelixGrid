# HelixGrid på Windows – nem installation

Denne guide er lavet til folk, der ikke vil bruge terminalen til daglig.

## Første installation

1. Download eller clone HelixGrid.
2. Find filen **windows-install.bat**.
3. Dobbeltklik på den.
4. Tryk **Ja** hvis Windows spørger om administrator-tilladelse.
5. Installeren kontrollerer og installerer automatisk:
   - WSL 2
   - Git
   - Python 3.13
   - Docker Desktop
   - HelixGrid Python-værktøjer
   - HelixGrid Docker-images
6. Hvis Windows skal genstartes, bliver installationen registreret til at fortsætte automatisk efter næste login.
7. Til sidst åbnes HelixGrid-dashboardet automatisk.

Installeren opretter også en **HelixGrid-genvej på skrivebordet** og i Start-menuen.

## Efter en genstart af computeren

Du behøver ikke installere noget igen.

Dobbeltklik enten på:

- **HelixGrid** på skrivebordet, eller
- **start.bat** i HelixGrid-mappen.

Dashboardet åbner uden PowerShell-terminal og forsøger automatisk at starte Docker Desktop og HelixGrid.

## Dashboardet

Øverst kan du se Docker-status, Coordinator-status, antal workers og antal workflows.

Du har knapper til **Start HelixGrid**, **Genstart** og **Stop**.

## Filer & Backup

På fanen **Filer & Backup** vælger du bare mapper med knapperne.

### Fil-audit

Klik **Start audit** for at finde største filer, byte-identiske dubletter, lave SHA-256 checksums og få en læsbar rapport.

Dine originale filer mountes read-only.

### Backup

Klik **Start backup** for at lave `backup.tar.gz` og `backup.json` med SHA-256 og information.

Resultater skrives i en separat resultatmappe.

## Workflows

Fanen **Workflows** viser dine jobs og deres status. Du kan opdatere listen og annullere et valgt workflow.

## Resultater

Fanen **Resultater** viser audit-rapporten direkte i dashboardet. Knappen **Åbn resultatmappe** åbner Windows Stifinder.

## Logs

Hvis noget går galt, åbner du fanen **Logs** og klikker **Hent logs**. Du behøver ikke skrive Docker-kommandoer manuelt.

## Indstillinger

Du kan vælge hvilken mappe HelixGrid må læse, hvor resultater skal gemmes, 1–16 worker-containere og om HelixGrid automatisk skal starte, når dashboardet åbner.

Arbejdsmappen og resultatmappen skal være separate mapper. Det forhindrer, at en writable resultatmount kan give adgang til de originale filer.

## Installationslog

Hvis installationen fejler, ligger loggen normalt her:

`%LOCALAPPDATA%\HelixGrid\install.log`

Du kan køre **windows-install.bat** igen. Installeren springer programmer over, som allerede er installeret.
