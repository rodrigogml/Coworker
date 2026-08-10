---
name: drive
description: Pesquisar, consultar, baixar, exportar, enviar, substituir, copiar, renomear, mover e colocar arquivos ou pastas na lixeira do Google Drive, além de listar drives compartilhados e administrar compartilhamentos autorizados. Usar quando a tarefa mencionar Google Drive, Meu Drive, drives compartilhados, arquivos online, pastas, upload, download, cópia ou permissões de compartilhamento.
---

# Operar Google Drive

Usar `python skills/drive/scripts/drive.py --profile NOME`. Obter OAuth por
`scripts/google_accounts.py`; nunca aceitar nem revelar tokens.

## Inicializar a configuração

Quando algum arquivo estiver ausente, inicializar os dois modelos sem sobrescrever:

```powershell
python scripts/integration_config.py init google
python scripts/integration_config.py init drive
```

## Executar

1. Se Drive ainda não estiver autorizado, não pedir credenciais da aplicação;
   executar `python scripts/google_accounts.py enroll --profile NOME --service drive`
   após a pessoa escolher o serviço e concluir o navegador local.
2. Pesquisar antes de abrir ou alterar arquivos.
3. Identificar arquivos por ID, não somente pelo nome.
4. Usar `files show` para verificar capacidades antes de mutações.
5. Usar `--dry-run` em upload, criação, movimentação e compartilhamento.
6. Não sobrescrever downloads sem `--overwrite`.
7. Não oferecer exclusão permanente; usar `files update --trashed`.
8. Alterar permissões somente com autorização explícita e destinatário verificado.
9. Tratar `files replace` como sobrescrita de conteúdo remoto.

```powershell
python skills/drive/scripts/drive.py --profile pessoal files list `
  --query "name contains 'Relatório' and trashed = false"
python skills/drive/scripts/drive.py --profile pessoal files download `
  --id ID --output data/work/arquivo.pdf
python skills/drive/scripts/drive.py --profile pessoal files upload `
  --source data/work/arquivo.pdf --parent-id ID --dry-run
python skills/drive/scripts/drive.py --profile pessoal files update `
  --id ID --name "Novo nome.pdf" --dry-run
python skills/drive/scripts/drive.py --profile pessoal files copy `
  --id ID --name "Cópia.pdf" --parent-id PASTA --dry-run
python skills/drive/scripts/drive.py --profile pessoal drives list
python skills/drive/scripts/drive.py --profile pessoal permissions list --file-id ID
```

Arquivos Google Docs, Sheets ou Slides devem ser obtidos com `files export`; arquivos
binários usam `files download`. Não repetir escritas após timeout. Não improvisar
endpoints. Ler [references/api-contracts.md](references/api-contracts.md) quando
houver dúvida.
