# Recursos disponíveis

## Paginação comum

A maioria das listagens aceita:

- `--page N`: consulta uma página, iniciando em 1;
- `--all-pages`: percorre páginas até `max_pages`;
- `--only-api`: solicita somente registros importados pela API;
- `--changed-from DD/MM/AAAA` e `--changed-to DD/MM/AAAA`;
- `--only-created` ou `--only-changed`.

`transfers` e `account-entries` usam os filtros próprios de `ListarLancCC`, descritos
nas seções correspondentes.

## Empresas

```powershell
python skills/omie/scripts/omie.py companies list
python skills/omie/scripts/omie.py companies show --id 123
```

A saída omite certificados, CSCs, senhas e configurações fiscais sensíveis.

## Clientes e fornecedores

```powershell
python skills/omie/scripts/omie.py customers list
python skills/omie/scripts/omie.py customers show --id 123
python skills/omie/scripts/omie.py customers show --integration-id ERP-123
```

Em trabalhos Telegram restritos, `customers prepare` gera o envelope tipado dentro de
`COWORKER_JOB_DERIVED`. Pesquise antes pelo CPF/CNPJ e use o arquivo somente depois de
autorização explícita, primeiro em `customers create --dry-run`.

As mutações disponíveis são `create`, `update`, `deactivate` e `delete`. Consultar
[operations.md](operations.md) antes de preparar o envelope.

## Projetos

```powershell
python skills/omie/scripts/omie.py projects list
python skills/omie/scripts/omie.py projects show --id 123
python skills/omie/scripts/omie.py projects show --integration-id PROJ-123
```

As mutações disponíveis são `create`, `update`, `deactivate` e `delete`.

## Categorias, departamentos e contas correntes

```powershell
python skills/omie/scripts/omie.py categories show --code 2.01.01
python skills/omie/scripts/omie.py departments show --code DEP-1
python skills/omie/scripts/omie.py current-accounts show --id 123
python skills/omie/scripts/omie.py current-accounts show --integration-id BANCO-1
```

Esses recursos são somente de leitura e servem também para validar referências de
títulos, baixas, lançamentos diretos e transferências. A saída de categorias inclui
os marcadores de receita, despesa, transferência, totalização e disponibilidade.

## Produtos

```powershell
python skills/omie/scripts/omie.py products list --description "texto"
python skills/omie/scripts/omie.py products show --id 123
python skills/omie/scripts/omie.py products show --code SKU-001
```

O filtro de descrição usa correspondência contendo.

## Contas a pagar e receber

Ambos aceitam `--issued-from`, `--issued-to`, `--customer-id` e `--status`:

```powershell
python skills/omie/scripts/omie.py payables list --status ATRASADO
python skills/omie/scripts/omie.py receivables show --id 123
```

Contas a pagar aceitam `create`, `update`, `delete`, `pay` e `cancel-payment`.
Em `update`, `data.reconcile` altera o status de conciliação do documento como
booleano (`true`/`false`).
Contas a receber aceitam `create`, `update`, `delete`, `receive`, `cancel-receipt`,
`reconcile` e `unreconcile`.

## Lançamentos diretos em conta

```powershell
python skills/omie/scripts/omie.py --profile EMPRESA account-entries list --nature expense
python skills/omie/scripts/omie.py --profile EMPRESA account-entries list --nature revenue
python skills/omie/scripts/omie.py --profile EMPRESA account-entries show --id 123
python skills/omie/scripts/omie.py --profile EMPRESA account-entries show --integration-id ID
```

`list` exige `--nature`: `expense` filtra lançamentos manuais `EXTP` e `revenue`
filtra `EXTR`. As mutações disponíveis são `create`, `update` e `delete`; consultar
[operations.md](operations.md) antes de preparar o envelope.

## Transferências entre contas

```powershell
python skills/omie/scripts/omie.py transfers list
python skills/omie/scripts/omie.py transfers show --id 123
python skills/omie/scripts/omie.py transfers show --integration-id TRANSF-123
```

As mutações disponíveis são `create`, `update` e `delete`. A listagem usa a
paginação própria de `ListarLancCC`, traduzida para o mesmo formato de saída dos
demais recursos.

## Pedidos de venda

```powershell
python skills/omie/scripts/omie.py sales-orders list `
  --customer-id 123 --status FATURADO
python skills/omie/scripts/omie.py sales-orders show --id 456
```

A listagem solicita o resumo do pedido para reduzir volume e exposição de dados.

## Ordens de serviço

```powershell
python skills/omie/scripts/omie.py service-orders list --status N
python skills/omie/scripts/omie.py service-orders show --number 1001
```

Na consulta individual, tarefas vinculadas não são solicitadas por padrão.
