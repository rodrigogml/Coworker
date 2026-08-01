---
name: omie
description: Consultar dados do ERP Omie por API, incluindo empresas, clientes e fornecedores, produtos, contas a pagar, contas a receber, pedidos de venda e ordens de serviço. Use quando a tarefa mencionar Omie, ERP, cadastro de clientes, produtos, títulos financeiros, pedidos ou ordens de serviço armazenados no Omie.
---

# Consultar o ERP Omie

Usar exclusivamente `python skills/omie/scripts/omie.py`. O script lê uma única
entrada do KeePassXC, usando `Username` como App Key e `Password` como App Secret, e
devolve somente campos operacionais selecionados.
Quando houver várias empresas, selecionar a referência com `--profile NOME`.

## Inicializar a configuração

```powershell
python scripts/integration_config.py init omie
```

O comando cria somente `data/config/omie.toml` quando ele ainda não existe. Se o
arquivo já existir, informa `already_exists` e preserva seu conteúdo.

## Respeitar o escopo

- Tratar todos os comandos atuais como somente de leitura.
- Não enviar chamadas arbitrárias à API, mesmo que um método exista na documentação.
- Não executar inclusão, alteração, exclusão, baixa, conciliação, faturamento,
  cancelamento, emissão fiscal, geração de boleto ou PIX.
- Ampliar a skill somente após documentar o contrato e a autorização da operação.
- Nunca inserir App Key ou App Secret em argumentos, arquivos de parâmetros, logs ou
  respostas.

## Executar consultas

1. Usar `doctor` para diagnosticar autenticação quando necessário.
2. Escolher o recurso mais específico.
3. Preferir `show` quando houver um identificador inequívoco.
4. Usar filtros de data e uma página por vez antes de solicitar `--all-pages`.
5. Examinar `pagination.truncated`; continuar por `--page` quando necessário.
6. Interromper após um erro de contrato. Chamadas inválidas repetidas podem bloquear a
   integração.

```powershell
python skills/omie/scripts/omie.py doctor

python skills/omie/scripts/omie.py companies list
python skills/omie/scripts/omie.py customers list
python skills/omie/scripts/omie.py customers show --id 123456
python skills/omie/scripts/omie.py products list --description "Produto"
python skills/omie/scripts/omie.py products show --code SKU-001

python skills/omie/scripts/omie.py payables list `
  --issued-from 01/07/2026 --issued-to 31/07/2026
python skills/omie/scripts/omie.py receivables list --status ATRASADO

python skills/omie/scripts/omie.py sales-orders list `
  --customer-id 123456 --status FATURADO
python skills/omie/scripts/omie.py service-orders show --number 1001
```

## Interpretar limites

- A Omie usa `POST` com `call`, `param`, `app_key` e `app_secret`; não é uma API REST.
- Cada página aceita no máximo 100 registros.
- `--all-pages` respeita `max_pages` da configuração privada.
- O HTTP 425 indica bloqueio temporário após chamadas inválidas; não repetir.
- O HTTP 429 indica limite de consumo; aguardar antes de tentar novamente.
- Ler [references/api-contracts.md](references/api-contracts.md) para autenticação,
  limites e endpoints.
- Ler [references/resources.md](references/resources.md) para seletores, filtros e
  campos resumidos de cada recurso.
