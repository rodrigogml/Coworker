---
name: cloudflare
description: Gerenciar a conta Cloudflare do usuário por API, incluindo validar o token, listar e criar zonas, consultar, criar, alterar e excluir registros DNS e ligar ou desligar o proxy. Use quando a tarefa mencionar Cloudflare, zonas, DNS autoritativo, registros A/AAAA/CNAME/TXT/MX ou nuvem laranja/cinza.
---

# Gerenciar Cloudflare

Use exclusivamente `python skills/cloudflare/scripts/cloudflare.py` para
acessar a API. O script obtém internamente a credencial referenciada em
`data/config/cloudflare.toml` do cofre KeePassXC e nunca a devolve ao agente.
Quando houver várias contas, selecionar a referência com `--profile NOME`.

## Aplicar a autorização

- Executar consultas de leitura dentro do escopo pedido sem confirmação adicional.
- Executar uma alteração quando o pedido atual autorizar de forma explícita a operação
  e identificar exatamente seus alvos e valores.
- Pedir ao usuário os dados ausentes quando zona, conta, registro, valor ou estado
  desejado forem ambíguos.
- Não usar uma confirmação produzida pelo próprio agente ou um código gerado pelo
  script como autorização humana.
- Usar `--dry-run` quando o usuário pedir uma prévia ou quando ela ajudar a resolver
  uma incerteza técnica. Não tratar o `--dry-run` como autorização.
- Exigir autorização explícita e atual para excluir um registro DNS.
- Não excluir zonas: esta skill deliberadamente não oferece essa operação.

## Executar o fluxo

1. Usar `python skills/cloudflare/scripts/cloudflare.py doctor` quando for
   necessário diagnosticar autenticação ou permissões.
2. Consultar zonas ou registros para resolver IDs e verificar o estado atual.
3. Recusar seleção ambígua; preferir `--record-id` quando houver registros repetidos.
4. Executar a alteração já autorizada, sem uma segunda cerimônia artificial.
5. Examinar o JSON retornado e informar se houve mudança, estado já satisfatório,
   simulação ou falha.

Não usar `curl`, não consultar diretamente o KeePassXC e não imprimir variáveis de
ambiente ou cabeçalhos de autenticação.

## Comandos

```powershell
python skills/cloudflare/scripts/cloudflare.py doctor
python skills/cloudflare/scripts/cloudflare.py zones list
python skills/cloudflare/scripts/cloudflare.py zones list --name exemplo.com
python skills/cloudflare/scripts/cloudflare.py zones create `
  --name exemplo.com --account-id <account_id>

python skills/cloudflare/scripts/cloudflare.py dns list --zone exemplo.com
python skills/cloudflare/scripts/cloudflare.py dns list `
  --zone exemplo.com --name www --type A
python skills/cloudflare/scripts/cloudflare.py dns show `
  --zone exemplo.com --name www --type A

python skills/cloudflare/scripts/cloudflare.py dns create `
  --zone exemplo.com --type A --name www --content 192.0.2.10 --proxied
python skills/cloudflare/scripts/cloudflare.py dns update `
  --zone exemplo.com --name www --type A --content 192.0.2.20
python skills/cloudflare/scripts/cloudflare.py dns proxy `
  --zone exemplo.com --name www --type A --enable
python skills/cloudflare/scripts/cloudflare.py dns proxy `
  --zone exemplo.com --record-id <record_id> --disable
python skills/cloudflare/scripts/cloudflare.py dns delete `
  --zone exemplo.com --record-id <record_id>
```

Acrescentar `--dry-run` aos comandos de criação, alteração, proxy ou exclusão para
validar e mostrar a operação sem enviá-la.

Nomes de registro podem ser completos, relativos à zona ou `@` para a raiz. O comando
é idempotente onde possível: criação idêntica e estado de proxy já atendido retornam
`changed: false`.

## Interpretar limites

- O token precisa das permissões Cloudflare correspondentes à operação.
- Somente registros que a API marque como `proxiable: true` aceitam mudança de proxy.
- A criação de uma zona normalmente a deixa pendente até que os nameservers sejam
  configurados no registrador.
- Consultar [references/api-contracts.md](references/api-contracts.md) para endpoints,
  permissões e comportamento operacional.
