# Comandos BIS2

Use sempre `python skills/bis2/scripts/bis2.py`.

## Consultas

```powershell
python skills/bis2/scripts/bis2.py doctor
python skills/bis2/scripts/bis2.py --profile example doctor
python skills/bis2/scripts/bis2.py --profile example companies-list
python skills/bis2/scripts/bis2.py --profile example certificates-list
python skills/bis2/scripts/bis2.py --profile example certificates-list --company-id 2
python skills/bis2/scripts/bis2.py --profile example companies-list --limit 100 --offset 0
python skills/bis2/scripts/bis2.py --profile example items-list --status ACTIVE --displayline "%cafe%" --limit 500 --offset 0
python skills/bis2/scripts/bis2.py --profile example items-list --id 5404
python skills/bis2/scripts/bis2.py --profile example items-list --id 18726 --code-id 18497
python skills/bis2/scripts/bis2.py --profile example item-update --item-id 5404 --field description --value "Descricao revisada" --confirm
python skills/bis2/scripts/bis2.py --profile example item-update --item-id 5404 --code-id 123 --field ncm --value 12345678 --confirm
python skills/bis2/scripts/bis2.py --profile example item-price-update --company-id 2 --code-id 123 --price 19.90 --confirm
python skills/bis2/scripts/bis2.py --profile example nfce-detail --id 164129
python skills/bis2/scripts/bis2.py --profile example nfce-detail --serie 1 --number 123
python skills/bis2/scripts/bis2.py --profile example nfce-listagem-chaves --company-id 2 --certificate-id 6 --start 2026-07-01T00:00:00 --end 2026-07-31T23:59:59
python skills/bis2/scripts/bis2.py --profile example nfce-listagem-chaves --status SEFAZPROBLEM --start 2026-07-01T00:00:00 --end 2026-07-31T23:59:59 --limit 500
python skills/bis2/scripts/bis2.py --profile example nfce-listagem-chaves --status ERROR --start 2026-07-01T00:00:00 --end 2026-07-31T23:59:59 --limit 500
# --status aceita exatamente os valores de DocFiscalVO.DocFiscalStatus:
# SELLING STORED SOLD CANCELLING CANCELED ERROR ERROR_SYNC VOID
# SEFAZVALIDATING SEFAZPROBLEM SEFAZOFFLINE
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
python skills/bis2/scripts/bis2.py --profile example doc-fiscal-repair --doc-id 123 --set total=15.90 --item-set 456:cbenef=RJ123456 --confirm
```

Operações de escrita exigem `--profile` explícito para reduzir risco de executar em servidor errado.

Todas as listagens retornam metadados de paginação no campo `pagination`:
`complete`, `truncated`, `limit`, `offset`, `next_offset`, `total` e `total_known`.
O limite máximo é 500. Quando `complete` for falso, consulte a próxima página usando
`--offset` igual a `next_offset`; nunca interprete uma página truncada como ausência de registros.
## Recuperação de NFC-e com problema de envio

São duas operações independentes e devem ser executadas nesta ordem, depois de
confirmar o documento e corrigir seus dados no banco:

1. `nfce-fix-envi-xml` chama `DocFiscalCrud.fixNFCeEnviXML(id)`, reconstrói o XML
   com os dados persistidos e muda o documento de `SEFAZPROBLEM` para
   `SEFAZOFFLINE`.
2. `nfce-send-offline` chama o fluxo de envio de contingência para a SEFAZ. Ele
   espera o XML pronto e o status `SEFAZOFFLINE`.

O segundo comando não substitui o primeiro quando o XML está desatualizado.
