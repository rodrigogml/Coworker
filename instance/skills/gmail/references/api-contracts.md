# Contratos da API do Gmail

Revisado em 2026-07-30.

## Autenticação

- OAuth 2.0 para aplicação desktop.
- Cadastro: `python scripts/google_accounts.py enroll --profile NOME`.
- Cliente OAuth: `APIs/Google/OAuthClient`, com Client ID em `Username` e Client
  Secret em `Password`.
- Conta: entrada indicada em `config/google.example.toml`, com e-mail em `Username` e
  refresh token em `Password`.
- Access tokens são renovados em memória e nunca persistidos.

O perfil padrão usa `gmail.modify`, além de `openid` e `email`. Reduzir ou ampliar
escopos exige novo consentimento. Aplicações pessoais com menos de 100 usuários podem
operar sem verificação pública, mas continuam sujeitas às políticas de dados e à tela
de aplicação não verificada.

## Endpoints permitidos

Base: `https://gmail.googleapis.com/gmail/v1`.

| Recurso | Operações |
|---|---|
| Perfil | `GET /users/me/profile` |
| Mensagens | listar, mostrar, modificar labels, lixeira e restaurar |
| Threads | listar e mostrar |
| Labels | listar |
| Rascunhos | listar, mostrar, criar e enviar |

Exclusão permanente e configurações da conta não são expostas.

## Pesquisa e leitura

`messages list` e `threads list` aceitam a sintaxe de pesquisa do Gmail em `q`.
Resultados listados possuem apenas IDs; usar `show` para conteúdo.

Formatos de mensagem:

- `minimal`: IDs e labels;
- `metadata`: cabeçalhos, sem corpo;
- `full`: conteúdo estruturado;
- `raw`: mensagem RFC 2822 codificada em base64url.

## Escritas

- `drafts create` recebe um arquivo RFC 822 e não envia.
- `drafts send` envia um rascunho existente.
- `messages modify` adiciona ou remove IDs de labels.
- `messages trash` é reversível por `messages untrash`.

Fontes oficiais:

- <https://developers.google.com/identity/protocols/oauth2/native-app>
- <https://developers.google.com/workspace/gmail/api/reference/rest>
- <https://developers.google.com/workspace/gmail/api/auth/scopes>
- <https://developers.google.com/workspace/gmail/api/guides/filtering>
- <https://developers.google.com/workspace/gmail/api/guides/drafts>
