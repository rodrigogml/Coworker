# Comandos BIS2

Use sempre `python skills/bis2/scripts/bis2.py`.

## Consultas

```powershell
python skills/bis2/scripts/bis2.py doctor
python skills/bis2/scripts/bis2.py --profile example doctor
python skills/bis2/scripts/bis2.py --profile example nfce-listagem-chaves --company-id 2 --certificate-id 6 --start 2026-07-01T00:00:00 --end 2026-07-31T23:59:59
python skills/bis2/scripts/bis2.py --profile example nfce-download-xml --company-id 2 --certificate-id 6 --key 00000000000000000000000000000000000000000000
```

`nfce-download-xml` sem `--confirm` executa `dryRun`.

## Escritas

Exigir autorização explícita e atual antes de executar:

```powershell
python skills/bis2/scripts/bis2.py --profile example nfce-download-xml --company-id 2 --certificate-id 6 --key 00000000000000000000000000000000000000000000 --confirm
python skills/bis2/scripts/bis2.py --profile example nfce-inutilize-number --company-id 2 --certificate-id 6 --serie 1 --number-start 123 --number-end 125 --confirm
python skills/bis2/scripts/bis2.py --profile example nfce-send-offline --doc-id 123 --confirm
python skills/bis2/scripts/bis2.py --profile example update-doc-fiscal-status --serie 1 --number 123 --status ERROR
```

Operações de escrita exigem `--profile` explícito para reduzir risco de executar em servidor errado.
