---
name: omie
description: Consultar e manipular dados permitidos do ERP Omie por API, incluindo clientes e fornecedores, projetos, categorias, departamentos, contas correntes, lançamentos diretos, contas a pagar e receber, baixas, conciliações e transferências entre contas. Use quando a tarefa mencionar Omie, ERP, contrapartes, projetos ou operações financeiras mantidas no Omie.
---

# Operar o ERP Omie

Usar exclusivamente `python skills/omie/scripts/omie.py`. O script obtém App Key e
App Secret internamente de uma única entrada do KeePassXC e nunca os aceita em
argumentos ou arquivos de entrada.

## Preparar a integração

Inicializar somente quando a configuração privada estiver ausente:

```powershell
python scripts/integration_config.py init omie
```

Quando houver mais de uma empresa, informar sempre `--profile NOME`. Toda escrita
exige o perfil explícito, mesmo que exista `default_profile`.

## Respeitar o escopo

- Consultar sem confirmação dentro do pedido atual.
- Exigir autorização explícita e atual antes de criar, alterar, inativar, excluir,
  baixar, cancelar, conciliar, desconciliar ou transferir.
- Não tratar `--dry-run` como autorização; ele é somente uma prévia técnica.
- Nunca improvisar `call`, endpoint ou campo fora da allowlist do script.
- Não usar `Upsert`; criações usam `request_id` estável e código de integração
  determinístico.
- Manter boleto, PIX, faturamento e emissão fiscal fora desta skill.

Em `payables update` e `receivables update`, `data.reconcile` é booleano e altera o
status de conciliação do documento (`true` conciliado, `false` não conciliado),
traduzido para `conciliar_documento = "S"/"N"`. As operações `receivables reconcile`
e `receivables unreconcile` continuam destinadas à baixa identificada pelo seletor.
Para `account-entries`, a API oficial de `AlterarLancCC` não expõe conciliação no
contrato de alteração; a skill não simula esse campo.

## Consultar

Usar `doctor` quando for necessário diagnosticar autenticação. Preferir `show`
com identificador inequívoco e limitar listagens antes de usar `--all-pages`.

```powershell
python skills/omie/scripts/omie.py --profile EMPRESA doctor
python skills/omie/scripts/omie.py --profile EMPRESA customers show --id 123
python skills/omie/scripts/omie.py --profile EMPRESA projects list
python skills/omie/scripts/omie.py --profile EMPRESA categories list
python skills/omie/scripts/omie.py --profile EMPRESA departments show --code DEP-1
python skills/omie/scripts/omie.py --profile EMPRESA current-accounts list
python skills/omie/scripts/omie.py --profile EMPRESA account-entries list --nature expense
python skills/omie/scripts/omie.py --profile EMPRESA account-entries list --nature expense --date-from 01/07/2026 --date-to 31/07/2026 --category 2.11.93 --observation-contains MEI
python skills/omie/scripts/omie.py --profile EMPRESA transfers prepare --request-id telegram:omie:transfer-1 --source-account-id 1 --destination-account-id 2 --date 20/07/2026 --amount 6000.00 --category-code 0.01.02 --observation "Transferência operacional"
python skills/omie/scripts/omie.py --profile EMPRESA transfers show --integration-id ID
```

Transferências exigem `--category-code` e uma categoria ativa, não totalizadora e
marcada pela Omie como transferência; categorias comuns de receita ou despesa são
rejeitadas antes da chamada de escrita. O marcador `nao_exibir=S` pode ser usado
pela Omie em categorias técnicas de transferência e não as invalida.

Quando um comprovante trouxer CPF/CNPJ e nome de uma contraparte ausente, pesquisar
primeiro por `customers` usando `tax_id`. Somente após autorização explícita usar o
preparador tipado, executar `customers create --dry-run` e então a criação real:

```powershell
python skills/omie/scripts/omie.py customers prepare `
  --request-id telegram:omie:contraparte-foster-1 `
  --legal-name "FOSTER LIMA LTDA" --tax-id 03.390.722/0001-98
python skills/omie/scripts/omie.py --profile EMPRESA customers create `
  --input-file CAMINHO_DEVOLVIDO --dry-run
```

O preparador aceita somente campos tipados, grava exclusivamente em
`COWORKER_JOB_DERIVED`, não acessa credenciais nem a API e é idempotente pelo
`request_id`. Nunca cadastrar uma contraparte apenas por inferência; quando ela for
obrigatória para um lançamento, mantê-lo incompleto até localizar ou cadastrar após
autorização.

Examinar `pagination.truncated` e continuar por `--page` quando necessário.

Usar `account-entries` para receitas ou despesas lançadas diretamente em uma conta,
sem criar título a pagar ou receber. Usar `payables`/`receivables` quando existir
obrigação ou direito financeiro, e `transfers` somente para movimentação entre duas
contas próprias.

O comando `account-entries update` aceita atualizações parciais de valores e campos
específicos (por exemplo `amount`, `date`, `category`, `project`, `departments`,
`document_number` e `observation`), preservando os campos omitidos.

## Alterar

Fornecer um envelope JSON versão 1 por arquivo UTF-8 ou stdin, nunca por flags de
campos. Colocar `--profile` antes do recurso:

```powershell
python skills/omie/scripts/omie.py --profile EMPRESA projects create `
  --input-file entrada.json --dry-run

Get-Content entrada.json -Raw | python skills/omie/scripts/omie.py `
  --profile EMPRESA payables create --input-stdin
```

No gateway Telegram restrito, não usar `apply_patch`, pipeline ou shell para criar o
envelope. Para uma criação simples de lançamento direto, executar o preparador fechado:

```powershell
python skills/omie/scripts/omie.py account-entries prepare `
  --request-id telegram:omie:identificador-estavel `
  --nature expense --account-id 123 --date 04/08/2026 --amount 150.00 `
  --document-type DEB --category-code 2.01.01 --project-id 456 `
  --department DEP-ADMIN:100
```

O comando não acessa credenciais nem a API. Ele cria exclusivamente dentro de
`COWORKER_JOB_DERIVED`, não sobrescreve conteúdo e devolve o caminho a usar em
`account-entries create --input-file CAMINHO`, primeiro com `--dry-run`. Confinamento,
nome idempotente e criação exclusiva são fornecidos pelo componente compartilhado
`interfaces.telegram.job_context`; não implementar uma segunda escrita dentro da skill.
Repetir `--department CODIGO:PERCENTUAL` para ratear entre departamentos; os
percentuais devem ser positivos, não podem repetir códigos e devem somar exatamente
100. O preparador mantém a categoria única informada por `--category-code`; essa opção
não cria rateio de categorias.

Usar o mesmo `request_id` ao retomar uma operação. Em lote, corrigir a falha e
reenviar o mesmo envelope: a skill valida todos os itens antes da primeira escrita,
executa em sequência e reconhece criações já existentes.

Inativar clientes, fornecedores e projetos antes da exclusão física. Toda exclusão
exige `confirm_delete: true`. Não excluir título com baixa ativa.

Se o resultado for `unknown`, consultar pelo identificador de integração antes de
repetir. A skill tenta essa recuperação automaticamente nas criações.

Ler [references/operations.md](references/operations.md) para envelopes, campos e
exemplos de escrita. Ler [references/resources.md](references/resources.md) para
seletores e consultas, e [references/api-contracts.md](references/api-contracts.md)
para endpoints, allowlist, limites e erros.
