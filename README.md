# BOTina

BOTina é uma assistente pessoal local, de identidade feminina, orientada por arquivos
de instruções e capaz de executar ferramentas de linha de comando com segurança.

O repositório contém apenas o núcleo reutilizável: instruções, skills, scripts,
migrations, testes e modelos. Memória, configurações locais, bancos e credenciais
permanecem em `data/`, fora do Git.

> [!IMPORTANT]
> O projeto está em estágio experimental e é **Windows-first**. Memória, instruções e
> integrações baseadas em Python são portáveis, mas o desbloqueio automático do cofre
> atualmente depende do Gerenciador de Credenciais do Windows. Provedores para macOS e
> Linux poderão ser adicionados no futuro.

## Capacidades atuais

- memória legível por humanos em Markdown;
- memória estruturada em SQLite;
- cofre KeePassXC sem exposição de segredos ao agente;
- gerenciamento de zonas e registros DNS da Cloudflare;
- gerenciamento de domínios e aliases do Forward Email;
- consultas ao ERP Omie;
- gerenciamento de tarefas, projetos, seções e etiquetas do Todoist;
- busca, leitura, criação e edição de notas do Notion;
- base para novas skills pessoais.

## Estrutura

```text
BOTina/
├── AGENTS.md
├── README.md
├── .gitignore
├── .agents/
├── config/
│   ├── cloudflare.example.toml
│   ├── forwardemail.example.toml
│   ├── omie.example.toml
│   ├── notion.example.toml
│   ├── todoist.example.toml
│   ├── memory-policy.yaml
│   └── secrets.example.toml
├── data/                         # privado e ignorado pelo Git
│   ├── config/
│   ├── memory/
│   ├── secrets/
│   └── memory.sqlite3
├── migrations/
├── scripts/
├── skills/
├── templates/
│   └── memory/
└── tests/
```

## Dependências

- Windows 10 ou 11 para a integração atual com o Gerenciador de Credenciais;
- Python 3.11 ou superior disponível como `python`;
- KeePassXC e `keepassxc-cli` para o cofre;
- Git apenas para versionar o núcleo público;
- acesso à internet somente para integrações que precisem de APIs.

SQLite já faz parte do Python; não é necessário instalar um executável separado.
As ferramentas atuais usam somente a biblioteca padrão do Python e não exigem
`pip install`.

Antes do bootstrap, a IA deve diagnosticar o ambiente:

```powershell
Get-Command python -ErrorAction SilentlyContinue
python --version
Get-Command git -ErrorAction SilentlyContinue
```

Se Python 3.11 ou superior não estiver disponível, a IA pode instalar somente quando
estiver autorizada. Caso não possa, deve orientar a instalação pelo
[site oficial do Python](https://www.python.org/downloads/windows/), pedir que a opção
de adicionar o Python ao `PATH` seja habilitada e retomar pelo comando
`python --version`.

Git não é necessário para executar a BOTina. Se a tarefa exigir versionamento e ele
estiver ausente, aplicar a mesma regra: instalar quando autorizado ou orientar a
instalação pelo [site oficial do Git](https://git-scm.com/download/win).

## Bootstrap da instância local

Uma IA deve executar estas etapas de forma idempotente. Arquivos existentes nunca
devem ser sobrescritos.

### 1. Criar a área privada

```powershell
New-Item -ItemType Directory -Force data/config, data/memory, data/secrets | Out-Null

$modelos = @{
  "config/secrets.example.toml" = "data/config/secrets.toml"
  "config/cloudflare.example.toml" = "data/config/cloudflare.toml"
  "config/forwardemail.example.toml" = "data/config/forwardemail.toml"
  "config/omie.example.toml" = "data/config/omie.toml"
  "config/notion.example.toml" = "data/config/notion.toml"
  "config/todoist.example.toml" = "data/config/todoist.toml"
  "templates/memory/README.md" = "data/memory/README.md"
  "templates/memory/profile.md" = "data/memory/profile.md"
  "templates/memory/preferences.md" = "data/memory/preferences.md"
}

foreach ($origem in $modelos.Keys) {
  $destino = $modelos[$origem]
  if (-not (Test-Path -LiteralPath $destino)) {
    Copy-Item -LiteralPath $origem -Destination $destino
  }
}
```

Esse bloco pula cada arquivo que já existir.

### 2. Ajustar a configuração local

O arquivo versionado `config/secrets.example.toml` é o modelo; sua cópia efetiva é
`data/config/secrets.toml`. O agente deve localizar `KeePassXC.exe` e
`keepassxc-cli.exe` e registrar seus caminhos na cópia efetiva. Esses valores não são
senhas, mas descrevem a instalação pessoal e permanecem fora do Git.

Usar esta ordem de descoberta:

1. caminhos já registrados em `data/config/secrets.toml`;
2. comandos encontrados pelo `PATH`;
3. instalação portátil em diretórios conhecidos da máquina;
4. instalação padrão do KeePassXC em `Program Files`.

O agente pode corrigir automaticamente caminhos comprovadamente encontrados na
configuração privada. Não deve gravar caminhos pessoais no modelo público, inventar
um caminho ou substituir outras configurações locais sem necessidade.

Scripts da BOTina são executados diretamente por seus caminhos no repositório. O
bootstrap não deve criar wrappers ou atalhos em diretórios externos.

Editar `data/config/cloudflare.toml` apenas quando a referência da credencial ou o
endpoint forem diferentes do modelo. Nunca gravar o token nesse arquivo.

Aplicar a mesma regra a `data/config/forwardemail.toml`: ele contém somente o endpoint,
a referência `APIs/ForwardEmail` e o tempo limite, nunca a chave.

`data/config/omie.toml` contém uma referência à entrada composta de App Key e App
Secret, limites de paginação e o endpoint oficial. Nunca gravar as credenciais nesse
arquivo.

`data/config/todoist.toml` contém somente o endpoint oficial, a referência
`APIs/Todoist` e limites de paginação. Nunca gravar o token nesse arquivo.

`data/config/notion.toml` contém somente o endpoint e a versão oficial da API, a
referência `APIs/Notion`, limites de paginação e o intervalo entre requisições. Nunca
gravar o token ou o conteúdo de notas nesse arquivo.

### 3. Inicializar a memória

```powershell
python scripts/memory.py init
python scripts/memory.py status
```

### 4. Preparar o KeePassXC

```powershell
python scripts/credential_vault.py status
python scripts/credential_vault.py create
python scripts/credential_vault.py enroll
```

`create` somente deve ser usado quando o cofre ainda não existir. `enroll` abre um
console separado para a pessoa usuária digitar a senha mestra; ela nunca deve ser
pedida na conversa.

Quando o KeePassXC não estiver disponível, a IA deve:

1. executar a ordem de descoberta da configuração local;
2. informar exatamente qual executável não foi encontrado;
3. instalar ou baixar somente quando o pedido atual autorizar essa ação;
4. usar a [fonte oficial do KeePassXC](https://keepassxc.org/download/) e validar a
   instalação;
5. quando não puder instalar, fornecer passos objetivos e aguardar a pessoa usuária.

### 5. Validar integrações

```powershell
python scripts/credential_vault.py status
python scripts/credential_vault.py check "APIs/CloudFlare"
python skills/cloudflare-manage/scripts/cloudflare.py doctor
python scripts/credential_vault.py check "APIs/ForwardEmail"
python skills/forward-email-manage/scripts/forward_email.py doctor
python scripts/credential_vault.py check "APIs/Omie"
python skills/omie-manage/scripts/omie.py doctor
python scripts/credential_vault.py check "APIs/Todoist"
python skills/todoist-manage/scripts/todoist.py doctor
python scripts/credential_vault.py check "APIs/Notion"
python skills/notion-manage/scripts/notion.py doctor
```

Se a entrada da Cloudflare não existir, a IA deve orientar:

```powershell
python scripts/credential_vault.py add "APIs/CloudFlare"
```

O token deve ser digitado exclusivamente na janela confidencial aberta pelo comando.

Se a entrada do Forward Email não existir, usar o mesmo fluxo:

```powershell
python scripts/credential_vault.py add "APIs/ForwardEmail"
```

Para a Omie, abrir a interface do KeePassXC:

```powershell
python scripts/credential_vault.py open
```

Criar uma única entrada `APIs/Omie`, preencher a App Key em `Username` e o App Secret
em `Password`. Os dois valores devem ser digitados somente na interface confidencial.

Se a entrada do Todoist não existir, usar o fluxo de inclusão confidencial:

```powershell
python scripts/credential_vault.py add "APIs/Todoist"
```

O token pessoal do Todoist deve ser digitado somente na janela aberta pelo comando.

Para o Notion, criar uma integração interna em
<https://www.notion.so/profile/integrations>, habilitar as capacidades de leitura,
inserção e atualização de conteúdo e compartilhar com ela as páginas desejadas pelo
menu de conexões do Notion. Depois, incluir o token confidencialmente:

```powershell
python scripts/credential_vault.py add "APIs/Notion"
```

O token deve ser digitado somente na janela aberta pelo comando. Uma integração
interna não enxerga automaticamente todo o espaço de trabalho: página não
compartilhada pode aparecer como inexistente.

## Como agir quando algo estiver ausente

A IA deve diferenciar três situações:

- **Arquivo local ausente:** criar diretórios e copiar o modelo correspondente.
- **Dependência ausente:** detectar caminhos e versões; instalar somente com
  autorização, ou orientar a instalação manual.
- **Interação confidencial ou login:** abrir a interface apropriada e solicitar que a
  pessoa usuária conclua a etapa fora da conversa.

Não inventar valores pessoais, não substituir arquivos locais e não guardar segredo em
Markdown, TOML, SQLite, logs ou argumentos de linha de comando.

## Memória

Arquivos Markdown privados ficam em `data/memory/`. O formato recomendado é:

```markdown
### Título objetivo

- Tipo: fato informado | inferência | decisão
- Origem: usuário | arquivo | sistema
- Atualizado em: YYYY-MM-DD
- Escopo: global | projeto | tarefa

Conteúdo da memória.
```

A memória SQLite usa `data/memory.sqlite3`:

```powershell
python scripts/memory.py remember `
  --kind preference `
  --subject "Comunicação" `
  --content "Prefere respostas em português." `
  --source "usuário"

python scripts/memory.py search "português"
```

Senhas e tokens são recusados. Apenas referências externas podem ser registradas com
`--credential-ref`.

## Cloudflare

```powershell
python skills/cloudflare-manage/scripts/cloudflare.py zones list
python skills/cloudflare-manage/scripts/cloudflare.py dns list --zone exemplo.com
python skills/cloudflare-manage/scripts/cloudflare.py dns proxy `
  --zone exemplo.com --name www --type A --enable
```

Consultas podem ser feitas diretamente dentro do escopo solicitado. Alterações exigem
autorização explícita na conversa. `--dry-run` é uma prévia técnica, não uma
confirmação.

## Forward Email

```powershell
python skills/forward-email-manage/scripts/forward_email.py account show
python skills/forward-email-manage/scripts/forward_email.py domains list
python skills/forward-email-manage/scripts/forward_email.py domains verify `
  --domain exemplo.com
python skills/forward-email-manage/scripts/forward_email.py aliases list `
  --domain exemplo.com
python skills/forward-email-manage/scripts/forward_email.py aliases create `
  --domain exemplo.com --name contato `
  --recipient destino@exemplo.net
```

Consultas podem ser feitas dentro do escopo solicitado. Criações, alterações e
exclusões exigem autorização explícita na conversa. `--dry-run` é uma prévia técnica,
não uma confirmação. Envio de mensagens e geração de senhas não fazem parte da skill.

## Omie

```powershell
python skills/omie-manage/scripts/omie.py companies list
python skills/omie-manage/scripts/omie.py customers show --id 123456
python skills/omie-manage/scripts/omie.py products list --description "Produto"
python skills/omie-manage/scripts/omie.py receivables list --status ATRASADO
python skills/omie-manage/scripts/omie.py sales-orders list `
  --customer-id 123456 --status FATURADO
```

A primeira versão é somente de leitura e cobre empresas, clientes, produtos, contas a
pagar, contas a receber, pedidos de venda e ordens de serviço. A skill não oferece
chamada arbitrária: cada método permitido foi classificado e possui saída resumida
para evitar exposição acidental de credenciais, certificados ou corpos excessivos.

## Todoist

```powershell
python skills/todoist-manage/scripts/todoist.py projects list
python skills/todoist-manage/scripts/todoist.py tasks list --project-id ID
python skills/todoist-manage/scripts/todoist.py tasks create `
  --content "Enviar relatório" --due-string "amanhã às 10h"
python skills/todoist-manage/scripts/todoist.py tasks close --id ID
```

A skill usa a API unificada v1 e cobre tarefas, projetos, seções e etiquetas.
Alterações exigem autorização explícita na conversa. Exclusões de tarefas, seções e
projetos podem remover conteúdo descendente; arquivar deve ser preferido quando
atender ao pedido.

## Notion

```powershell
python skills/notion-manage/scripts/notion.py pages search --query "Reunião"
python skills/notion-manage/scripts/notion.py pages get --id ID
python skills/notion-manage/scripts/notion.py pages find-content `
  --query "termo interno" --max-pages 10
python skills/notion-manage/scripts/notion.py pages create `
  --parent-page-id ID --title "Nova nota" --markdown-file data/work/nova-nota.md
```

A API busca títulos, não o texto completo das páginas. `find-content` implementa uma
varredura controlada: seleciona páginas acessíveis, lê o Markdown e devolve somente
trechos correspondentes. Criações e substituições recebem conteúdo por arquivos
UTF-8 privados; alterações pontuais usam uma lista JSON de `old_str` e `new_str`.
Envio à lixeira é reversível, e exclusão permanente não faz parte da skill.

## Privacidade e publicação

O conteúdo de `data/` é pessoal, ainda que algum arquivo isolado pareça inofensivo.
Nunca usar `git add --force` nessa pasta. Antes de cada publicação:

```powershell
git status --short --ignored
git diff --cached
```

Revise também o histórico e execute uma ferramenta de detecção de segredos. Cofres
KeePassXC, bancos SQLite, `.env`, chaves e configurações locais são ignorados.

## Testes

```powershell
python -m unittest discover -s tests -v
```

O workflow público executa esses testes no GitHub Actions com Windows e Python 3.11 e
3.12. Ele não recebe acesso ao conteúdo local de `data/`, ao cofre ou a credenciais.
