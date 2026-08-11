# Codex CLI interativo

A partir da pasta `instance/`, execute:

```powershell
python scripts/codex_interactive.py
```

Também existem atalhos equivalentes na raiz de `instance/`:

```text
codex-interactive.bat
codex-interactive.ps1
```

Os lançadores não aceitam argumentos de configuração: sempre usam a configuração
privada da própria instância, em `data/config/telegram.toml`.

O lançador lê `data/config/telegram.toml` e reutiliza o adaptador do gateway para
definir o executável, `CODEX_HOME`, diretório de operação, sandbox, rede, política
de aprovação, modelo e diretórios adicionais. As regras são sincronizadas antes da
abertura do TUI interativo.

O backend `app-server` usado pelo Telegram não altera o comportamento deste comando:
ele abre deliberadamente o modo interativo do `codex`.
