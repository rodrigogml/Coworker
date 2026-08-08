---
name: contacts
description: Listar, pesquisar, consultar, criar, corrigir e excluir contatos pessoais e grupos do Google pela People API. Usar quando a tarefa mencionar Google Contacts, agenda de contatos, telefone, endereço de e-mail, organização, pessoa, grupo ou dados de contato associados a uma conta Google.
---

# Operar Google Contacts

Usar `python skills/contacts/scripts/contacts.py --profile NOME`. Obter OAuth por
`scripts/google_accounts.py`; nunca aceitar nem revelar tokens.

## Inicializar a configuração

Quando algum arquivo estiver ausente, inicializar os dois modelos sem sobrescrever:

```powershell
python scripts/integration_config.py init google
python scripts/integration_config.py init contacts
```

## Executar

1. Pesquisar antes de criar para evitar duplicidades.
2. Identificar contatos pelo `resourceName`, nunca somente pelo nome.
3. Ler o contato atual antes de atualizar; o script preserva o `etag` da fonte.
4. Usar `--dry-run` antes de criar, alterar ou excluir.
5. Alterar ou excluir somente com autorização explícita e atual.
6. Não executar mutações paralelas para uma mesma conta.
7. Ao excluir um grupo, preservar seus contatos; o script fixa
   `deleteContacts=false`.

```powershell
python skills/contacts/scripts/contacts.py --profile pessoal contacts search `
  --query "Maria"
python skills/contacts/scripts/contacts.py --profile pessoal contacts show `
  --resource-name people/ID
python skills/contacts/scripts/contacts.py --profile pessoal contacts create `
  --name "Example Person" --email "person@example.com" --dry-run
python skills/contacts/scripts/contacts.py --profile pessoal contacts update `
  --resource-name people/ID --phone "+55 11 99999-9999" --dry-run
python skills/contacts/scripts/contacts.py --profile pessoal groups list
python skills/contacts/scripts/contacts.py --profile pessoal groups members `
  --group-resource contactGroups/ID --add people/ID --dry-run
```

A exclusão é permanente para os dados de contato e requer cuidado adicional. Não
repetir escritas após timeout. Não improvisar endpoints. Ler
[references/api-contracts.md](references/api-contracts.md) quando houver dúvida.
