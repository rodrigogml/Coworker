# Contratos da API Forward Email

Fonte oficial: [Forward Email API](https://forwardemail.net/en/email-api).

## Autenticação e transporte

- Base URL: `https://api.forwardemail.net`.
- Autenticação HTTP Basic: chave da API como nome de usuário e senha vazia.
- A chave é lida do KeePassXC pelo cliente e nunca é aceita como argumento.
- Requisições de escrita usam `application/x-www-form-urlencoded`.
- Listagens usam páginas de 50 itens e o cabeçalho `X-Page-Count` quando disponível.

## Endpoints usados

| Recurso | Método | Endpoint |
| --- | --- | --- |
| Conta | `GET` | `/v1/account` |
| Domínios | `GET`, `POST` | `/v1/domains` |
| Domínio | `GET`, `PUT`, `DELETE` | `/v1/domains/:domain` |
| Verificar DNS | `GET` | `/v1/domains/:domain/verify-records` |
| Verificar SMTP | `GET` | `/v1/domains/:domain/verify-smtp` |
| Aliases | `GET`, `POST` | `/v1/domains/:domain/aliases` |
| Alias | `GET` | `/v1/domains/:domain/aliases/:id-or-name` |
| Alias | `PUT`, `DELETE` | `/v1/domains/:domain/aliases/:id` |

O cliente resolve domínios e aliases por igualdade exata antes de qualquer escrita.
Quando um nome não identifica um alias de modo único, deve ser usado o ID.

## Recursos excluídos desta versão

- Envio de e-mail: produz comunicação externa e requer um fluxo próprio de composição,
  revisão e autorização.
- Geração de senha: o endpoint devolve uma senha em texto na resposta, incompatível
  com a regra de nunca expor segredos ao agente.
- Download de logs: a documentação informa que a solicitação pode iniciar uma tarefa
  em segundo plano e enviar e-mail, portanto não é tratada como leitura sem efeito.
- Calendários, contatos e caixas postais: devem ser adicionados em uma extensão futura
  somente após modelar saída segura e autorização.
