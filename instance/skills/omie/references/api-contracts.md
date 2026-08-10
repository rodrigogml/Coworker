# Contratos da API Omie

## Fontes oficiais

- [Portal do Desenvolvedor](https://developer.omie.com.br/)
- [Lista de APIs](https://developer.omie.com.br/service-list/)
- [Características e recomendações](https://ajuda.omie.com.br/pt-BR/articles/5412721-caracteristicas-e-recomendacoes-das-apis-do-omie)
- [Limites de consumo](https://ajuda.omie.com.br/pt-BR/articles/8112984-limites-de-consumo-da-api-do-omie)

## Transporte e autenticação

Toda operação JSON usa `POST` no endpoint do serviço:

```json
{
  "call": "ListarClientes",
  "app_key": "<obtida do cofre>",
  "app_secret": "<obtida do cofre>",
  "param": [
    {
      "pagina": 1,
      "registros_por_pagina": 100
    }
  ]
}
```

As credenciais fazem parte do corpo exigido pela Omie. O cliente fixa HTTPS e o host
`app.omie.com.br` para impedir envio a um destino configurado indevidamente.

No KeePassXC, o par é armazenado em uma única entrada: o campo `Username` contém a
App Key e o campo `Password` contém o App Secret. A configuração privada registra
somente a referência dessa entrada.

## Serviços permitidos

| Recurso | Endpoint | Leitura | Escrita permitida |
| --- | --- | --- | --- |
| Empresas | `/geral/empresas/` | `ListarEmpresas`, `ConsultarEmpresa` | — |
| Clientes/fornecedores | `/geral/clientes/` | `ListarClientes`, `ConsultarCliente` | `IncluirCliente`, `AlterarCliente`, `ExcluirCliente` |
| Projetos | `/geral/projetos/` | `ListarProjetos`, `ConsultarProjeto` | `IncluirProjeto`, `AlterarProjeto`, `ExcluirProjeto` |
| Categorias | `/geral/categorias/` | `ListarCategorias`, `ConsultarCategoria`; `natureza` também é exposto como `observacao` | — |
| Departamentos | `/geral/departamentos/` | `ListarDepartamentos`, `ConsultarDepartamento` | — |
| Contas correntes | `/geral/contacorrente/` | `ListarContasCorrentes`, `ConsultarContaCorrente` | — |
| Produtos | `/geral/produtos/` | `ListarProdutos`, `ConsultarProduto` | — |
| Contas a pagar | `/financas/contapagar/` | `ListarContasPagar`, `ConsultarContaPagar` | incluir, alterar, excluir, lançar e cancelar pagamento |
| Contas a receber | `/financas/contareceber/` | `ListarContasReceber`, `ConsultarContaReceber` | incluir, alterar, excluir, lançar/cancelar recebimento, conciliar e desconciliar |
| Lançamentos diretos | `/financas/contacorrentelancamentos/` | `ListarLancCC`, `ConsultaLancCC` | `IncluirLancCC`, `AlterarLancCC`, `ExcluirLancCC` para origens `EXTP`/`EXTR`; alteração de conta em `APEP` |
| Transferências | `/financas/contacorrentelancamentos/` | `ListarLancCC`, `ConsultaLancCC` | `IncluirLancCC`, `AlterarLancCC`, `ExcluirLancCC` com tipo `TRA` |
| Pedidos de venda | `/produtos/pedido/` | `ListarPedidos`, `ConsultarPedido` | — |
| Ordens de serviço | `/servicos/os/` | `ListarOS`, `ConsultarOS` | — |

O script rejeita qualquer método fora desta allowlist. Métodos `Upsert`, operações
de boleto/PIX, faturamento e emissões fiscais não são expostos.

## Módulos não expostos

O catálogo oficial também oferece CRM, boletos e PIX, compras,
estoque e produção, pedidos e faturamento, documentos fiscais, NF-e, serviços, NFS-e
e painel do contador. Eles não foram expostos porque misturam
contratos extensos com efeitos financeiros, fiscais ou de comunicação externa.

Para acrescentar uma capacidade:

1. confirmar endpoint, método, parâmetros e retorno na documentação oficial atual;
2. classificar a operação como leitura, alteração cadastral, financeira ou fiscal;
3. adicionar método fixo à lista permitida, nunca uma chamada arbitrária;
4. validar identificadores e parâmetros antes da requisição;
5. criar um resumo com campos permitidos em vez de devolver o corpo integral;
6. implementar idempotência ou `--dry-run` quando aplicável;
7. cobrir autenticação, sanitização, erros e efeitos com testes;
8. exigir autorização explícita e atual para qualquer escrita.

## Paginação e limites

- Máximo de 100 registros por página.
- Limites gerais publicados: 960 requisições por minuto por IP; 240 por minuto por
  IP, App Key e método; quatro simultâneas por IP, App Key e método.
- Consultas redundantes ao mesmo ID dentro de 60 segundos podem ser recusadas.
- Dez requisições incorretas para a mesma combinação podem gerar bloqueio de 30
  minutos, devolvido como HTTP 425.

O cliente opera sequencialmente, não repete automaticamente chamadas com erro e limita
o percurso de páginas.

## Erros

Falhas podem chegar por status HTTP ou objeto com `faultcode` e `faultstring`. O
cliente extrai somente a mensagem curta, remove as credenciais defensivamente e nunca
imprime o corpo original do erro.

## Compras e documentos fiscais

Os serviÃ§os oficiais de Nota de Entrada (`produtos/notaentrada/`), Recebimento de NF-e
(`produtos/recebimentonfe/`) e Documentos Fiscais (`contador/xml/`) possuem allowlists
separadas no script. Nota de Entrada expÃµe inclusÃ£o, alteraÃ§Ã£o, consulta, listagem,
status e exclusÃ£o; Recebimento expÃµe consulta, listagem, alteraÃ§Ã£o, mudanÃ§a de etapa,
conclusÃ£o, reversÃ£o e exclusÃ£o. Documentos Fiscais Ã© somente leitura.

Os retornos fiscais sÃ£o reduzidos antes da saÃ­da: XML, DANFE, links e anexos nÃ£o sÃ£o
devolvidos. A listagem de documentos informa apenas metadados e disponibilidade.
Quando `cGeraFinanceiro = "S"`, `cCodParc`/`nQtdeParc` seguem o contrato da Nota de
Entrada; os tÃ­tulos gerados devem ser localizados e verificados apÃ³s a escrita, nunca
inferidos a partir do retorno de inclusÃ£o.
