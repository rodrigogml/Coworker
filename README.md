# Coworker

Coworker é um núcleo versionado para instâncias pessoais e operacionais.

- O runtime comum fica em [`instance/`](instance/).
- O guia de instalação e operação fica em [`instance/README.md`](instance/README.md).
- Dados privados de cada instância ficam em `instance/data/` e não são versionados.
- Regras de desenvolvimento ficam em [`AGENTS.md`](AGENTS.md); regras de execução ficam
  em [`instance/AGENTS.md`](instance/AGENTS.md).

Para configurar uma instância no Windows, execute:

```powershell
./install.ps1
```

Para detalhes de configuração, integração e operação, consulte o README dentro de
`instance/`.
