# Contratos da API

- Bases: `https://www.googleapis.com/drive/v3` e
  `https://www.googleapis.com/upload/drive/v3`.
- Arquivos e pastas são recursos `files`; pastas usam MIME
  `application/vnd.google-apps.folder`.
- Upload binário usa `multipart/related`.
- Substituição de conteúdo usa upload `PATCH`; cópia usa
  `POST /files/{fileId}/copy`.
- Movimentação usa `addParents` e `removeParents`.
- Drives compartilhados são listados por `GET /drives`.
- A skill não chama `files.delete` nem `emptyTrash`.
- Compartilhamentos usam `permissions`; notificações ficam desativadas salvo
  `--notify`, e alterações de função usam `PATCH`.
- Escopo usado: `https://www.googleapis.com/auth/drive`.

Fontes oficiais:

- https://developers.google.com/workspace/drive/api/reference/rest/v3/files
- https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions
- https://developers.google.com/workspace/drive/api/guides/search-files
