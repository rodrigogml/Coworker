# Contratos da API do Todoist

## Fonte oficial e autenticação

- [API unificada v1](https://developer.todoist.com/api/v1/)
- [Portal para desenvolvedores](https://developer.todoist.com/)

Usar `https://api.todoist.com/api/v1` e enviar o token exclusivamente no cabeçalho
`Authorization: Bearer`. O token pessoal fica no campo `Password` da entrada
`APIs/Todoist` do KeePassXC.

Não usar os endpoints antigos `/rest/v1`, `/rest/v2` ou `/sync/v9`.

## Recursos expostos

| Recurso | Leitura | Escrita |
| --- | --- | --- |
| Tarefas | listar, consultar | criar, atualizar, mover, concluir, reabrir, excluir |
| Projetos | listar, consultar | criar, atualizar, arquivar, desarquivar, excluir |
| Seções | listar, consultar | criar, atualizar, arquivar, desarquivar, excluir |
| Etiquetas | listar, consultar | criar, atualizar, excluir |

As escritas usam `POST`, exceto exclusões, que usam `DELETE`. O script mantém caminhos
e operações em uma lista fechada.

## Paginação

Listagens retornam `results` e `next_cursor`. Enviar o cursor opaco seguinte com os
mesmos filtros. O limite máximo documentado é 200 registros por página.

Não persistir cursores. Durante paginação, alterações concorrentes podem duplicar ou
omitir objetos; o script elimina IDs duplicados ao percorrer `--all-pages` e respeita
`max_pages`.

## Efeitos importantes

- Concluir uma tarefa recorrente agenda a próxima ocorrência.
- Excluir uma tarefa remove suas subtarefas.
- Excluir uma seção remove suas tarefas.
- Excluir um projeto remove suas seções e tarefas.
- Arquivar preserva o objeto e é preferível à exclusão quando atende ao pedido.
- Prioridades vão de `1` (mais alta) a `4` (mais baixa).

O Todoist limita corpos `POST` a 1 MiB. O cliente não repete escritas automaticamente,
evitando efeitos duplicados quando a resposta for perdida.
