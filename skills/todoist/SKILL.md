---
name: todoist
description: Consultar e organizar o Todoist por API, incluindo tarefas, projetos, seções e etiquetas. Usar quando a tarefa mencionar Todoist, lista de tarefas, prioridades, prazos, conclusão, reabertura, projetos, seções ou etiquetas armazenadas no Todoist.
---

# Gerenciar o Todoist

Usar exclusivamente `python skills/todoist/scripts/todoist.py`. Obter o token
da entrada `APIs/Todoist` do KeePassXC e nunca aceitá-lo em argumentos ou respostas.
Quando houver várias contas, selecionar a referência com `--profile NOME`.

## Executar o fluxo

1. Usar `doctor` quando for necessário diagnosticar autenticação.
2. Consultar projetos e seções antes de escolher destinos por ID.
3. Preferir filtros e uma página; usar `--all-pages` somente quando necessário.
4. Usar `--dry-run` para inspecionar tecnicamente uma alteração quando houver dúvida.
5. Executar uma escrita somente quando o pedido atual autorizar a ação e identificar
   seus alvos.
6. Validar o objeto devolvido antes de informar a conclusão.

```powershell
python skills/todoist/scripts/todoist.py doctor
python skills/todoist/scripts/todoist.py projects list
python skills/todoist/scripts/todoist.py sections list --project-id ID
python skills/todoist/scripts/todoist.py labels list
python skills/todoist/scripts/todoist.py tasks list --project-id ID
python skills/todoist/scripts/todoist.py tasks show --id ID
```

## Manipular tarefas

```powershell
python skills/todoist/scripts/todoist.py tasks create `
  --content "Enviar relatório" --due-string "amanhã às 10h" --priority 2

python skills/todoist/scripts/todoist.py tasks update `
  --id ID --content "Enviar relatório revisado"

python skills/todoist/scripts/todoist.py tasks move `
  --id ID --section-id SECAO

python skills/todoist/scripts/todoist.py tasks close --id ID
python skills/todoist/scripts/todoist.py tasks reopen --id ID
```

`close` conclui tarefas normais e agenda a próxima ocorrência das recorrentes.
`delete` remove também subtarefas e exige pedido explícito e inequívoco.

## Manipular organização

Usar `create`, `update`, `archive`, `unarchive` e `delete` nos recursos que os
oferecem. A exclusão de projeto remove suas seções e tarefas; a exclusão de seção
remove suas tarefas. Tratar ambas como destrutivas.

```powershell
python skills/todoist/scripts/todoist.py projects create --name "Pessoal"
python skills/todoist/scripts/todoist.py sections create `
  --project-id ID --name "Esta semana"
python skills/todoist/scripts/todoist.py labels create --name "aguardando"
```

## Respeitar contratos

- Não improvisar endpoints ou chamadas arbitrárias.
- Não repetir automaticamente escritas após erro ou timeout.
- Não persistir cursores; usá-los apenas na paginação atual.
- Interpretar prioridade conforme a API v1: `1` é a mais alta e `4` a mais baixa.
- Ler [references/api-contracts.md](references/api-contracts.md) quando houver dúvida
  sobre endpoints, paginação, efeitos ou campos aceitos.
