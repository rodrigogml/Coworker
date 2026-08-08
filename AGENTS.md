# Coworker — Desenvolvimento

Este diretório é a raiz de desenvolvimento do projeto. O runtime executado pelas
instâncias fica em [`instance/`](instance/).

## Escopo

- Preserve a separação entre código público em `instance/` e dados privados em
  `instance/data/`.
- Não coloque credenciais, tokens, bancos locais ou configurações de uma instância
  neste diretório.
- Alterações de runtime devem ser implementadas dentro de `instance/` e acompanhadas
  de testes e documentação.
- O contrato de execução da instância está em [`instance/AGENTS.md`](instance/AGENTS.md).

## Validação

Execute a suíte adequada a partir da raiz do repositório. Os testes devem referenciar
o runtime em `instance/`; não crie cópias paralelas das skills, scripts ou interfaces.

O `AGENTS.md` da raiz descreve somente o processo de desenvolvimento. Ele não deve ser
usado como instrução operacional pelo gateway de uma instância.
