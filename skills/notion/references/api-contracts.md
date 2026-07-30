# Contratos da API do Notion

Referência revisada em 2026-07-30 para a versão `2026-03-11`.

## Autenticação e acesso

- Base permitida: `https://api.notion.com/v1`.
- Autenticação: `Authorization: Bearer <token>`.
- Toda chamada envia `Notion-Version: 2026-03-11`.
- A conexão deve ter acesso explícito às páginas. Em conexões internas, compartilhar
  a página pelo menu de conexões; páginas-filhas herdam esse acesso.
- Leitura Markdown exige a capacidade de leitura de conteúdo.
- Criação exige inserção de conteúdo; edição exige atualização de conteúdo.

## Endpoints permitidos

| Operação | Método e caminho |
|---|---|
| Diagnóstico | `GET /users/me` |
| Busca por título | `POST /search` |
| Ler Markdown | `GET /pages/{page_id}/markdown` |
| Criar página | `POST /pages` |
| Editar/substituir Markdown | `PATCH /pages/{page_id}/markdown` |
| Lixeira/restauração | `PATCH /pages/{page_id}` |

Não há suporte a caminho ou método arbitrário.

## Busca

`POST /search` pesquisa títulos entre páginas e fontes de dados compartilhadas com a
conexão; não pesquisa o corpo das páginas. A skill filtra objetos `page`.

`pages find-content` pagina essa busca, lê o Markdown de cada página candidata e faz a
comparação local. O limite de páginas é obrigatório e a saída contém apenas trechos
curtos. Páginas truncadas ou com blocos desconhecidos são sinalizadas.

## Edição Markdown

Para edição pontual:

```json
{
  "type": "update_content",
  "update_content": {
    "content_updates": [
      {"old_str": "texto atual", "new_str": "texto novo"}
    ]
  }
}
```

Para substituição integral:

```json
{
  "type": "replace_content",
  "replace_content": {"new_str": "# Conteúdo novo"}
}
```

As duas formas são recomendadas pelo Notion. A inserção incremental legada não é
exposta. `allow_deleting_content` permanece desabilitado para proteger páginas-filhas
e bancos incorporados.

## Limites e falhas

- A API aplica em média três requisições por segundo por integração.
- Requisições têm limite geral de 500 KB e 1.000 blocos; a skill limita cada arquivo
  Markdown a menos de 500 KB para reservar espaço ao JSON.
- Em `429`, respeitar `Retry-After`; a skill não repete escritas automaticamente.
- `403` normalmente indica capacidade ausente.
- `404 object_not_found` também pode significar que a página não foi compartilhada.
- `update_content` falha se `old_str` não existir; se houver várias ocorrências, a
  skill não solicita substituição global.

Fontes oficiais:

- <https://developers.notion.com/reference/authentication>
- <https://developers.notion.com/reference/post-search>
- <https://developers.notion.com/reference/retrieve-a-page-as-markdown>
- <https://developers.notion.com/reference/update-page-markdown>
- <https://developers.notion.com/reference/create-a-page>
- <https://developers.notion.com/reference/archive-delete-a-page>
- <https://developers.notion.com/reference/request-limits>
