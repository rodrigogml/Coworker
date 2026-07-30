# Recursos disponíveis

## Paginação comum

Todas as listagens aceitam:

- `--page N`: consulta uma página, iniciando em 1;
- `--all-pages`: percorre páginas até `max_pages`;
- `--only-api`: solicita somente registros importados pela API;
- `--changed-from DD/MM/AAAA` e `--changed-to DD/MM/AAAA`;
- `--only-created` ou `--only-changed`.

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

As consultas não realizam baixa, pagamento, recebimento ou conciliação.

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
