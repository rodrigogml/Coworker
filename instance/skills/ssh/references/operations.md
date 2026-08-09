# Contrato da skill SSH

O perfil privado fica em `data/config/ssh.toml` e não deve ser versionado.
Use `profile set`, nunca edição livre do TOML.

```powershell
python skills/ssh/scripts/ssh.py --profile turing doctor
python skills/ssh/scripts/ssh.py --profile turing check
```

Para cadastrar uma entrada ausente, use o broker protegido do gateway Telegram:

```powershell
python interfaces/telegram/scripts/request_credential.py `
  --entry Infraestrutura/Turing/SSH `
  --prompt "Cadastrar credencial SSH" `
  --field username:"Usuário SSH" `
  --field password:Passphrase `
  --field attachment:"Chave privada" `
  --attachment-name id_ed25519
```

O arquivo é recebido como documento, gravado diretamente no KeePassXC e removido
do armazenamento temporário ao final. Use `/cancel` para interromper.

Para chave sem passphrase, omita o campo `password` e solicite somente
`username` e `attachment`. Em uma captura com `password`, envie `/skip` quando
a passphrase estiver vazia.

`doctor` não abre conexão. `check` usa exclusivamente `uname -a`, com timeout,
host-key policy controlada e saída limitada. A chave é materializada em arquivo
temporário 0600 e removida em `finally`; passphrase não entra em argumentos,
logs ou saída.
