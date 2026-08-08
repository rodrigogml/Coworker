# Contratos usados da API Cloudflare

Base configurada: `https://api.cloudflare.com/client/v4`.

| Capacidade | Método e endpoint | Permissão relevante |
|---|---|---|
| Validar token | `GET /user/tokens/verify` | O próprio token |
| Listar zonas | `GET /zones` | Zone Zone Read |
| Criar zona | `POST /zones` | Zone Zone Edit ou Zone DNS Edit |
| Listar registros | `GET /zones/{zone_id}/dns_records` | DNS Read |
| Consultar registro | `GET /zones/{zone_id}/dns_records/{record_id}` | DNS Read |
| Criar registro | `POST /zones/{zone_id}/dns_records` | DNS Write |
| Alterar registro | `PATCH /zones/{zone_id}/dns_records/{record_id}` | DNS Write |
| Excluir registro | `DELETE /zones/{zone_id}/dns_records/{record_id}` | DNS Write |

## Regras operacionais

- Usar API Token com escopo mínimo; não usar Global API Key.
- Paginar listagens até `result_info.total_pages`.
- Resolver nomes por igualdade exata e rejeitar múltiplos resultados.
- Usar `PATCH` para preservar campos não alterados do registro.
- Tratar `success: false` como falha mesmo quando houver resposta HTTP.
- Expor ao agente somente `code` e `message` dos erros da API.
- Nunca registrar ou devolver o cabeçalho `Authorization`.

## Referências oficiais

- [Verify Token](https://developers.cloudflare.com/api/resources/user/subresources/tokens/methods/verify/)
- [List Zones](https://developers.cloudflare.com/api/resources/zones/methods/list/)
- [Create Zone](https://developers.cloudflare.com/api/resources/zones/methods/create/)
- [List DNS Records](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/list/)
- [Create DNS Record](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/create/)
- [Update DNS Record](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/edit/)
- [Delete DNS Record](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/delete/)
