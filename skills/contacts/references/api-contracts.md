# Contratos da API

- Base: `https://people.googleapis.com/v1`.
- Listagem: `GET /people/me/connections`.
- Pesquisa: `GET /people:searchContacts`, precedida por aquecimento com consulta vazia.
- Criação: `POST /people:createContact`.
- Atualização: `PATCH /people/{id}:updateContact`; exige `metadata.sources` atual e
  `updatePersonFields`.
- Exclusão: `DELETE /people/{id}:deleteContact`; é permanente para o contato.
- Grupos: `GET|POST /contactGroups`, `GET|PUT|DELETE /contactGroups/{id}` e
  `POST /contactGroups/{id}/members:modify`.
- Exclusão de grupo sempre usa `deleteContacts=false`.
- Escopo usado: `https://www.googleapis.com/auth/contacts`.
- Mutações da mesma conta devem ser sequenciais.

Fontes oficiais:

- https://developers.google.com/people/api/rest/v1/people.connections/list
- https://developers.google.com/people/api/rest/v1/people/searchContacts
- https://developers.google.com/people/api/rest/v1/people/updateContact
- https://developers.google.com/people/api/rest/v1/people/deleteContact
- https://developers.google.com/people/api/rest/v1/contactGroups
- https://developers.google.com/people/api/rest/v1/contactGroups.members/modify
