# Operações de escrita

## Envelope

Toda mutação recebe exatamente um documento JSON:

```json
{
  "schema_version": 1,
  "request_id": "origem:processo:identificador-estavel",
  "selector": {"id": 123},
  "data": {},
  "confirm_delete": true
}
```

- `request_id` deve permanecer igual em tentativas e retomadas da mesma operação.
- Usar somente um entre `selector.id` e `selector.integration_id`.
- Exclusões exigem `confirm_delete: true`.
- Para lote, substituir `selector`, `data` e `confirm_delete` por `items`. Cada item
  aceita esses três campos e recebe uma derivação estável do `request_id` do lote.
- Campos desconhecidos são recusados em todos os níveis validados.

```json
{
  "schema_version": 1,
  "request_id": "importacao-agosto-2026",
  "items": [
    {"data": {"name": "Projeto A"}},
    {"data": {"name": "Projeto B"}}
  ]
}
```

## Clientes e fornecedores

Usar o recurso compartilhado `customers`. A Omie mantém cliente e fornecedor no
mesmo cadastro; sua utilização posterior define o papel da contraparte.

```json
{
  "schema_version": 1,
  "request_id": "fornecedor-2026-001",
  "data": {
    "razao_social": "Fornecedor Exemplo Ltda",
    "nome_fantasia": "Fornecedor Exemplo",
    "cnpj_cpf": "00.000.000/0001-00",
    "email": "financeiro@example.com"
  }
}
```

Comandos: `create`, `update`, `deactivate` e `delete`. `update` recebe somente os
campos alterados; a skill consulta e recompõe o contrato. `deactivate` não recebe
`data`. `delete` somente aceita registro já inativo.

## Projetos

Comandos: `create`, `update`, `deactivate` e `delete`.

```json
{
  "schema_version": 1,
  "request_id": "projeto-reforma-matriz",
  "data": {"name": "Reforma da matriz"}
}
```

Em `update`, usar `name` e/ou `inactive`. A inclusão sempre cria projeto ativo e o
código de integração é derivado automaticamente.

## Referências financeiras

Contrapartes, projetos e contas correntes aceitam um seletor:

```json
{"id": 123}
{"integration_id": "CODIGO"}
{"name": "Nome exato"}
```

Contrapartes também aceitam `{"tax_id": "CPF-ou-CNPJ"}`. Categorias e departamentos
aceitam `{"code": "CODIGO"}` ou `{"name": "Nome exato"}`; o código pode ser
informado diretamente como texto. Nomes normalizam somente caixa e espaços. Zero ou
múltiplas correspondências são erro. Novas operações recusam referências inativas.

## Contas a pagar e receber

Usar `payables create|update|delete` ou
`receivables create|update|delete`. Campos financeiros expostos:

- `counterparty`, `due_date`, `amount`, `forecast_date` e `current_account`;
- `category` ou `categories`;
- `departments`, `project`, `document_number`, `installment_number`;
- `document_type`, `fiscal_document_number`, `issue_date` e `observation`;
- `installments`, somente em `create`.

Em `update`, `reconcile` é um booleano: `true` marca o documento como conciliado e
`false` remove a conciliação. O update continua parcial e preserva campos omitidos.

```json
{
  "schema_version": 1,
  "request_id": "nf-123-fornecedor-x",
  "data": {
    "counterparty": {"tax_id": "00000000000000"},
    "amount": "300.00",
    "category": {"code": "2.01.01"},
    "departments": [
      {"code": "DEP-OPERACAO", "percentage": "70.00"},
      {"code": "DEP-ADMIN", "percentage": "30.00"}
    ],
    "current_account": {"id": 12345},
    "project": {"name": "Reforma da matriz"},
    "installments": [
      {"due_date": "10/08/2026", "amount": "100.00"},
      {"due_date": "10/09/2026", "amount": "200.00"}
    ]
  }
}
```

A soma das parcelas deve ser igual a `amount`. A numeração `NNN/NNN` e os códigos
de integração por parcela são gerados pela skill. Rateios usam exclusivamente
`amount` ou `percentage` em todos os itens e devem fechar no valor do título ou em
100%. Em parcelamento, rateios herdados por valor devem fechar no valor de cada
parcela; preferir percentuais ou informar o rateio em cada parcela.

### Baixas e conciliações

`payables pay` e `receivables receive` usam o seletor do título e:

```json
{
  "schema_version": 1,
  "request_id": "baixa-titulo-123-20260802",
  "selector": {"id": 123},
  "data": {
    "current_account": {"id": 456},
    "amount": "100.00",
    "date": "02/08/2026",
    "discount": "0.00",
    "interest": "0.00",
    "fine": "0.00",
    "reconcile": false,
    "observation": "Baixa autorizada"
  }
}
```

O valor pode ser parcial, mas não pode exceder o saldo aberto. Cancelamentos usam
`cancel-payment` ou `cancel-receipt`; conciliação e desconciliação existem somente
em `receivables` como `reconcile` e `unreconcile`. Nessas quatro operações, o
`selector` identifica a baixa, não o título.

## Lançamentos diretos em conta

Usar `account-entries` para receitas ou despesas realizadas diretamente em uma conta,
sem gerar título a pagar ou receber. Criação e alteração exigem `nature` igual a
`expense` ou `revenue`; a skill consulta as categorias e confirma a natureza antes da
gravação.

Quando a operação partir do gateway Telegram restrito, preparar uma criação simples
sem depender de edição de arquivo ou stdin do shell:

```powershell
python skills/omie/scripts/omie.py account-entries prepare `
  --request-id telegram:omie:tarifa-20260804 `
  --nature expense --account-id 123 --date 04/08/2026 --amount 150.00 `
  --document-type DEB --category-code 2.01.01 `
  --project-id 456 --department DEP-ADMIN:100 `
  --observation "Tarifa bancária"
```

O preparador aceita ainda `--counterparty-id` e `--document-number`. Ele monta somente
uma categoria, sem rateio de categorias. Para departamentos, repetir
`--department CODIGO:PERCENTUAL`; cada percentual deve estar entre zero (exclusivo) e
100 (inclusivo), os códigos não podem se repetir e a soma deve fechar exatamente em
100. Por exemplo, `--department INFRA:60 --department OPERACOES:40` gera
`departments` com os dois rateios no contrato público da skill.

O JSON é criado com nome determinístico dentro do `derived/` do trabalho atual;
repetição idêntica reutiliza o arquivo e conteúdo divergente para o mesmo
`request_id` é recusado. O preparador não acessa credenciais nem a API; a existência e
atividade de cada departamento são verificadas posteriormente por `create --dry-run`
e `create`. O resultado não autoriza a escrita e deve ser consumido assim:

```powershell
python skills/omie/scripts/omie.py --profile EMPRESA account-entries create `
  --input-file CAMINHO_DEVOLVIDO --dry-run
python skills/omie/scripts/omie.py --profile EMPRESA account-entries create `
  --input-file CAMINHO_DEVOLVIDO
```

```json
{
  "schema_version": 1,
  "request_id": "tarifa-bancaria-20260804-001",
  "data": {
    "nature": "expense",
    "account": {"id": 123},
    "date": "04/08/2026",
    "amount": "150.00",
    "document_type": "DEB",
    "category": {"code": "2.01.01"},
    "counterparty": {"id": 456},
    "project": {"name": "Reforma da matriz"},
    "departments": [{"code": "DEP-ADMIN", "percentage": "100.00"}],
    "document_number": "TARIFA-08",
    "observation": "Tarifa bancária"
  }
}
```

Campos obrigatórios na criação: `nature`, `account`, `date`, `amount`,
`document_type` e exatamente um entre `category` e `categories`. O valor deve ser
positivo. Rateios devem usar somente valores ou somente percentuais e fechar no valor
do lançamento ou em 100%. `counterparty` é opcional: sua ausência cria o movimento
direto sem cliente ou fornecedor e sem gerar um título em aberto. Para representar um
fato já ocorrido, informar a data efetiva do movimento em `date`.

Tipos de documento permitidos: `ADI`, `BOL`, `CRT`, `CHQ`, `CON`, `CRE`, `DRF`,
`DAS`, `DEB`, `DIN`, `DOC`, `GUIA`, `PROT`, `REC`, `RPA`, `TED` e `99999`.
`TRA` pertence exclusivamente ao recurso `transfers`.

Uma alteração é parcial, mas sempre exige `nature` como declaração de segurança:

```json
{
  "schema_version": 1,
  "request_id": "tarifa-bancaria-20260804-ajuste",
  "selector": {"id": 12345},
  "data": {
    "nature": "expense",
    "amount": "145.00",
    "observation": null,
    "departments": []
  }
}
```

`null` remove `counterparty`, `project`, `document_number` ou `observation`;
`departments: []` remove o rateio departamental. Alterar o valor exige que rateios
preservados continuem fechando no novo total. Não é permitido converter despesa em
receita ou vice-versa: excluir e criar um novo lançamento exige autorizações próprias.

Alteração e exclusão aceitam somente lançamentos manuais diretos `EXTP` e `EXTR`,
inclusive os criados na interface Omie. Registros sem código de integração devem ser
selecionados por `id`. A exclusão exige `confirm_delete: true`.

`IncluirLancCC` não recebe campo de conciliação. Depois de uma criação real autorizada,
usar `show` e confirmar que `diversos.dDtConc` permanece vazio; essa verificação não
pode ser substituída pelo `dry-run`.

## Transferências entre contas

Usar `transfers list|show|create|update|delete`. A inclusão exige uma categoria de
transferência ativa e gera um único
`IncluirLancCC` de tipo `TRA`:

```json
{
  "schema_version": 1,
  "request_id": "transferencia-20260802-001",
  "data": {
    "source_account": {"id": 100},
    "destination_account": {"id": 200},
    "date": "02/08/2026",
    "amount": "1500.00",
    "category": {"code": "2.99.01"},
    "counterparty": {"id": 300},
    "project": {"id": 400},
    "departments": [{"code": "DEP-1", "percentage": "100.00"}],
    "document_number": "TRANSF-001",
    "observation": "Transferência entre contas"
  }
}
```

Origem e destino devem ser contas ativas e diferentes. `update` é parcial;
`delete` exige confirmação explícita no envelope.
