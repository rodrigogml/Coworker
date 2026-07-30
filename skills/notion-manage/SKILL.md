---
name: notion-manage
description: Buscar, ler, criar e editar notas e páginas do Notion por API. Usar quando a tarefa mencionar Notion, notas, páginas, pesquisa por título ou conteúdo, criação, edição, substituição, arquivamento na lixeira ou restauração de conteúdo armazenado no Notion.
---

# Gerenciar o Notion

Usar exclusivamente `python skills/notion-manage/scripts/notion.py`. Obter o token
da entrada `APIs/Notion` do KeePassXC e nunca aceitá-lo em argumentos ou respostas.

## Executar o fluxo

1. Usar `doctor` para diagnosticar autenticação e acesso.
2. Usar `pages search` para títulos; a busca nativa do Notion não pesquisa o corpo.
3. Usar `pages find-content` somente quando for necessário pesquisar dentro das notas.
4. Ler a página antes de uma edição para identificar o trecho exato e evitar ambiguidade.
5. Preferir `pages edit` para mudanças localizadas; usar `pages replace` apenas quando
   a substituição integral tiver sido solicitada.
6. Executar uma escrita somente quando o pedido atual autorizar a ação e identificar
   a página.
7. Validar o objeto devolvido antes de informar a conclusão.

```powershell
python skills/notion-manage/scripts/notion.py doctor
python skills/notion-manage/scripts/notion.py pages search --query "Reunião"
python skills/notion-manage/scripts/notion.py pages get --id ID
python skills/notion-manage/scripts/notion.py pages find-content `
  --query "termo interno" --max-pages 10
```

## Criar e editar

Conteúdo novo deve vir de arquivo UTF-8. Alterações pontuais devem vir de um arquivo
JSON contendo uma lista de pares `old_str` e `new_str`. Isso evita notas extensas ou
privadas em argumentos e no histórico do terminal.

```powershell
python skills/notion-manage/scripts/notion.py pages create `
  --parent-page-id ID --title "Nova nota" --markdown-file data/work/nova-nota.md

python skills/notion-manage/scripts/notion.py pages edit `
  --id ID --changes-file data/work/alteracoes.json

python skills/notion-manage/scripts/notion.py pages replace `
  --id ID --markdown-file data/work/nota-revisada.md

python skills/notion-manage/scripts/notion.py pages trash --id ID
python skills/notion-manage/scripts/notion.py pages restore --id ID
```

`pages edit` falha quando o trecho não existe ou aparece mais de uma vez. Não habilitar
remoção de páginas-filhas ou bancos dentro de uma nota sem ampliar explicitamente a
skill e obter autorização específica.

## Respeitar contratos

- Não improvisar endpoints ou chamadas arbitrárias.
- Não repetir automaticamente escritas após erro ou timeout.
- Não tratar `pages search` como busca de texto integral.
- Limitar `pages find-content`; ele lê cada página candidata e pode consumir muitas
  chamadas da API.
- Considerar `truncated` e `unknown_block_ids` ao interpretar Markdown incompleto.
- A lixeira é reversível; exclusão permanente não é oferecida por esta skill.
- Ler [references/api-contracts.md](references/api-contracts.md) quando houver dúvida
  sobre permissões, limites, busca ou corpos aceitos.
