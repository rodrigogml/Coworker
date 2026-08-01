# Coworker

Coworker é um núcleo reutilizável para instanciar assistentes pessoais locais,
orientadas por arquivos de instruções e capazes de executar ferramentas de linha de
comando com segurança. Nome, gênero gramatical, tom e bio são
definidos privadamente para cada instância.

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
- múltiplos perfis de autenticação por integração;
- busca, leitura, organização, rascunhos e envio autorizado pelo Gmail;
- consulta de disponibilidade e gestão de eventos no Google Calendar;
- pesquisa, transferência, organização e compartilhamento no Google Drive;
- pesquisa e manutenção de contatos pessoais pelo Google Contacts;
- consulta de contas digitais, PIX e linha digitável da CPFL;
- interface privada do Telegram com sessões persistentes do Codex CLI;
- base para novas skills pessoais.

## Estrutura

```text
Coworker/
├── AGENTS.md
├── README.md
├── install.ps1
├── install.sh
├── .gitignore
├── .agents/
├── config/
│   ├── cloudflare.example.toml
│   ├── calendar.example.toml
│   ├── contacts.example.toml
│   ├── cpfl.example.toml
│   ├── drive.example.toml
│   ├── forwardemail.example.toml
│   ├── gmail.example.toml
│   ├── google.example.toml
│   ├── identity.example.toml
│   ├── omie.example.toml
│   ├── notion.example.toml
│   ├── telegram.example.toml
│   ├── todoist.example.toml
│   ├── memory-policy.yaml
│   ├── secrets.example.toml
│   └── vault-entities.toml
├── data/                         # privado e ignorado pelo Git
│   ├── config/
│   │   └── identity.toml
│   ├── memory/
│   ├── secrets/
│   └── memory.sqlite3
├── migrations/
├── interfaces/
│   └── telegram/
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
- Codex CLI autônomo para a interface Telegram;
- dependências Python declaradas em `requirements.txt`;
- Git apenas para versionar o núcleo público;
- acesso à internet somente para integrações que precisem de APIs.

SQLite já faz parte do Python; não é necessário instalar um executável separado.
Instalar as dependências Python antes do bootstrap:

```powershell
python -m pip install -r requirements.txt
```

PyKeePass permite que a Coworker leia e grave atributos personalizados protegidos no
arquivo KDBX. As demais ferramentas continuam preferindo a biblioteca padrão.

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

Git não é necessário para executar a Coworker. Se a tarefa exigir versionamento e ele
estiver ausente, aplicar a mesma regra: instalar quando autorizado ou orientar a
instalação pelo [site oficial do Git](https://git-scm.com/download/win).

## Configuração de uma instância

O configurador cria e mantém a base da instância: identidade, configuração do cofre,
interface Telegram e sandbox do Codex. Configurações de skills e contas externas são
feitas posteriormente em conversa com a própria instância. O mesmo comando pode ser
executado novamente para revisar a configuração sem perder as respostas anteriores.

No Windows:

```powershell
.\install.ps1
```

No Linux ou macOS:

```sh
sh ./install.sh
```

Na primeira execução, o processo solicita nome, idioma, gênero gramatical, pronomes,
tom, estilo e bio para criar a identidade mínima. Em seguida, e em todas as execuções
posteriores, abre um menu principal que permite acessar diretamente identidade, cofre,
Codex CLI, Telegram ou memória, sem percorrer as seções anteriores. A opção `0`
produz um relatório somente de leitura com o estado de cada dependência, o motivo de
cada pendência e a ação necessária para resolvê-la.

O Codex CLI é obrigatório para a interface Telegram. Sua seção mostra o executável e
a versão encontrados, cria e grava explicitamente um `CODEX_HOME` privado para a
instância, verifica a autenticação dessa conta e permite revisar backend, sandbox,
rede, diretórios de leitura/escrita e tempo limite. O login iniciado pelo configurador
recebe `CODEX_HOME` somente no processo filho; ele não altera a variável global do
Windows nem reutiliza silenciosamente a autenticação do Codex Desktop.

O configurador também reutiliza caminhos válidos do KeePassXC, procura os executáveis
no `PATH` e em instalações conhecidas e, se não os localizar, solicita os caminhos.
Deixar os caminhos em branco mantém o cofre e as etapas dependentes como pendentes,
sem descartar a identidade nem impedir a inicialização da memória. Basta executar o
comando novamente depois de instalar ou localizar o KeePassXC.

`install.ps1` e `install.sh` executam o mesmo configurador Python, portanto menu,
validação, identidade e configuração do Codex são compartilhados entre Windows,
Linux e macOS. A detecção do KeePassXC usa os nomes e caminhos próprios de cada
sistema. O desbloqueio automático do cofre ainda usa o Gerenciador de Credenciais do
Windows; em outros sistemas, o relatório marca essa limitação e não libera a automação
de segredos do Telegram até existir um backend seguro equivalente.

O bot precisa ser criado previamente no BotFather; depois de o token ser guardado no
cofre, o configurador sincroniza o nome e as descrições públicas. O token é solicitado
em entrada mascarada e gravado diretamente no KeePassXC; não é necessário abrir ou
operar o cofre manualmente. O `@username`
permanece aquele definido no BotFather. Em seguida, o configurador abre o pareamento,
aguarda `/pair`, apresenta os IDs numéricos para confirmação local, aprova a pessoa
proprietária e inicia o gateway em segundo plano. Use `-NoStart` no PowerShell ou
`--no-start` no shell quando o processo for administrado por outro mecanismo.

O sandbox inicial concede leitura ao diretório do projeto, escrita somente em `data/`,
execução dos pontos de entrada públicos das skills e nenhuma rede. A rede pode ser
habilitada posteriormente, quando uma integração configurada realmente precisar dela.

## Bootstrap manual da instância local

Uma IA deve executar estas etapas de forma idempotente. Arquivos existentes nunca
devem ser sobrescritos.

### 1. Criar a área privada

```powershell
New-Item -ItemType Directory -Force data/config, data/memory, data/secrets | Out-Null

$modelos = @{
  "config/identity.example.toml" = "data/config/identity.toml"
  "config/secrets.example.toml" = "data/config/secrets.toml"
  "config/telegram.example.toml" = "data/config/telegram.toml"
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

Scripts da Coworker são executados diretamente por seus caminhos no repositório. O
bootstrap não deve criar wrappers ou atalhos em diretórios externos.

As configurações abaixo não fazem parte da instalação inicial. A própria instância
deve criar a cópia do modelo correspondente quando a pessoa usuária pedir a ativação
de uma skill. Por exemplo, editar `data/config/cloudflare.toml` apenas quando a referência da credencial ou o
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

Os modelos novos aceitam vários perfis:

```toml
default_profile = "pessoal"

[profiles.pessoal]
credential_ref = "APIs/Todoist/Pessoal"

[profiles.empresa]
credential_ref = "APIs/Todoist/Empresa"
```

Configurações antigas com uma única `credential_ref` continuam funcionando. Para usar
`--profile`, migrar o arquivo privado para o formato acima. Nomes, referências e
e-mails de contas ficam somente em `data/`; os modelos públicos usam `default`.

`data/config/google.toml` reúne endpoints OAuth oficiais, a referência do cliente
OAuth e os perfis de contas Google. Os arquivos `calendar.toml`, `contacts.toml`,
`drive.toml` e `gmail.toml` contêm somente parâmetros operacionais de suas APIs.
Client Secret, refresh tokens e access tokens nunca devem ser gravados nesses arquivos.

`data/config/cpfl.toml` relaciona um perfil Gmail à entrada protegida da pessoa titular.
O modelo público não contém nomes, unidades consumidoras ou referências pessoais.

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

### Cadastrar pessoas e organizações

O arquivo `config/vault-entities.toml` é o contrato público dos cadastros pessoais.
Ele define grupos, nomes de atributos, formatos e quais campos devem ser protegidos.
O arquivo contém somente o modelo, nunca valores reais.

Organizar as entradas assim:

```text
Pessoas/
├── Fisicas/
│   └── Nome civil completo
└── Juridicas/
    └── Razao social completa
```

Toda entrada usa `NOME_TIPO`, com valor `COMPLETO` ou `PARCIAL`. Quando o nome completo
não for conhecido, `REFERENCIA` também é obrigatória e integra o título entre
parênteses:

```text
Gustavo (Padaria Central)
João (Azulejista)
Marco Antônio (Salgadeiro)
```

Não usar aspas, dados sensíveis ou características passageiras como referência. Se
ainda houver colisão, acrescentar contexto estável, como
`Gustavo (Padaria Central - Centro)`. Os títulos devem ser únicos dentro de cada grupo,
pois as ferramentas da Coworker os acessam pelo caminho completo.

Quando o nome completo se tornar conhecido, atualizar `NOME_TIPO` para `COMPLETO`. A
referência pode continuar no título e no atributo `REFERENCIA` quando ainda ajudar na
identificação. Toda renomeação exige atualização das referências ao caminho da entrada.

Para pessoa física, usar também somente os atributos aplicáveis dentre `CPF`,
`DATA_NASCIMENTO`, `RG`, `RG_ORGAO_EMISSOR`, `RG_UF` e `NOME_SOCIAL`. Para pessoa
jurídica, usar `CNPJ`, `NOME_FANTASIA`, `INSCRICAO_ESTADUAL` e
`INSCRICAO_MUNICIPAL`. Campos opcionais desconhecidos podem permanecer ausentes; não
devem ser criados vazios.

> [!IMPORTANT]
> Marque na interface do KeePassXC todos os atributos que possuem
> `protected = true`. O cofre é criptografado em repouso, mas essa proteção adicional
> evita que uma consulta genérica mostre o valor quando o cofre estiver desbloqueado.

CPF e CNPJ são salvos somente com dígitos, datas usam `YYYY-MM-DD` e UFs usam duas
letras maiúsculas. Os campos padrão `Username`, `Password`, `URL` e `Notes` permanecem
vazios. Antes de adotar um novo atributo recorrente, acrescente sua definição ao
contrato para evitar grafias incompatíveis entre entradas.

O agente pode inspecionar e gravar os atributos sem revelar seus valores:

```powershell
python scripts/vault_entities.py inspect `
  --entry "Pessoas/Fisicas/Nome da Pessoa"

python scripts/vault_entities.py set `
  --entry "Pessoas/Fisicas/Nome da Pessoa" `
  --attribute CPF --prompt
```

`--prompt` é o fluxo indicado para digitação humana. Automações autorizadas podem usar
`--stdin`, nunca um argumento de linha de comando. O KeePassXC deve estar fechado
durante a gravação para impedir que duas aplicações salvem versões concorrentes do
mesmo cofre.

O cofre não substitui a memória relacional. SQLite e Markdown podem registrar que uma
pessoa é titular de uma unidade, a finalidade de uma conta e a referência da entrada
no KeePassXC. CPF, RG, CNPJ e demais valores protegidos permanecem exclusivamente no
cofre. Uma skill deve buscar somente o atributo necessário, usá-lo em memória durante
a operação autorizada e nunca exibi-lo ou copiá-lo para logs e bancos.

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
python skills/cloudflare/scripts/cloudflare.py doctor
python scripts/credential_vault.py check "APIs/ForwardEmail"
python skills/forwardemail/scripts/forward_email.py doctor
python scripts/credential_vault.py check "APIs/Omie"
python skills/omie/scripts/omie.py doctor
python scripts/credential_vault.py check "APIs/Todoist"
python skills/todoist/scripts/todoist.py doctor
python scripts/credential_vault.py check "APIs/Notion"
python skills/notion/scripts/notion.py doctor
python scripts/credential_vault.py check "APIs/Google/OAuthClient"
python scripts/google_accounts.py list
python scripts/google_accounts.py doctor --profile default
python skills/gmail/scripts/gmail.py --profile default doctor
python skills/calendar/scripts/calendar.py --profile default doctor
python skills/drive/scripts/drive.py --profile default doctor
python skills/contacts/scripts/contacts.py --profile default doctor
python skills/cpfl/scripts/cpfl.py --profile default doctor
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

### Preparar Google OAuth e Workspace

1. Criar um projeto no Google Cloud e configurar a tela de consentimento OAuth.
2. Habilitar Gmail API, Google Calendar API, Google Drive API e People API.
3. Criar um OAuth Client ID do tipo **Desktop app**.
4. Abrir o KeePassXC e criar `APIs/Google/OAuthClient`, usando o Client ID em
   `Username` e o Client Secret em `Password`.
5. Ajustar os perfis e escopos em `data/config/google.toml`.
6. Autorizar cada conta pelo navegador:

```powershell
python scripts/google_accounts.py enroll --profile default
```

O comando abre um console separado e o navegador. Após o consentimento, o refresh
token é escrito diretamente na entrada `APIs/Google/Accounts/Default`; ele não aparece
na conversa nem no terminal do agente. O perfil utiliza o e-mail autorizado em
`Username` e o refresh token em `Password`.

Para adicionar outra conta:

```toml
[profiles.empresa]
credential_ref = "APIs/Google/Accounts/Empresa"
scopes = [
  "openid",
  "email",
  "https://www.googleapis.com/auth/gmail.modify",
  "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
  "https://www.googleapis.com/auth/calendar.events",
  "https://www.googleapis.com/auth/calendar.freebusy",
  "https://www.googleapis.com/auth/drive",
  "https://www.googleapis.com/auth/contacts",
]
```

Executar então `python scripts/google_accounts.py enroll --profile empresa`.
Ao adicionar escopos a um perfil já autorizado, executar `enroll` novamente: um
refresh token antigo não recebe permissões adicionais automaticamente.
Aplicações pessoais com poucos usuários podem operar sem verificação pública, mas o
Google pode mostrar a tela de aplicação não verificada. Usar somente os escopos
necessários e observar as políticas de dados do Google Workspace.

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
python skills/cloudflare/scripts/cloudflare.py zones list
python skills/cloudflare/scripts/cloudflare.py dns list --zone exemplo.com
python skills/cloudflare/scripts/cloudflare.py dns proxy `
  --zone exemplo.com --name www --type A --enable
```

Consultas podem ser feitas diretamente dentro do escopo solicitado. Alterações exigem
autorização explícita na conversa. `--dry-run` é uma prévia técnica, não uma
confirmação.

## Forward Email

```powershell
python skills/forwardemail/scripts/forward_email.py account show
python skills/forwardemail/scripts/forward_email.py domains list
python skills/forwardemail/scripts/forward_email.py domains verify `
  --domain exemplo.com
python skills/forwardemail/scripts/forward_email.py aliases list `
  --domain exemplo.com
python skills/forwardemail/scripts/forward_email.py aliases create `
  --domain exemplo.com --name contato `
  --recipient destino@exemplo.net
```

Consultas podem ser feitas dentro do escopo solicitado. Criações, alterações e
exclusões exigem autorização explícita na conversa. `--dry-run` é uma prévia técnica,
não uma confirmação. Envio de mensagens e geração de senhas não fazem parte da skill.

## Omie

```powershell
python skills/omie/scripts/omie.py companies list
python skills/omie/scripts/omie.py customers show --id 123456
python skills/omie/scripts/omie.py products list --description "Produto"
python skills/omie/scripts/omie.py receivables list --status ATRASADO
python skills/omie/scripts/omie.py sales-orders list `
  --customer-id 123456 --status FATURADO
```

A primeira versão é somente de leitura e cobre empresas, clientes, produtos, contas a
pagar, contas a receber, pedidos de venda e ordens de serviço. A skill não oferece
chamada arbitrária: cada método permitido foi classificado e possui saída resumida
para evitar exposição acidental de credenciais, certificados ou corpos excessivos.

## Todoist

```powershell
python skills/todoist/scripts/todoist.py projects list
python skills/todoist/scripts/todoist.py tasks list --project-id ID
python skills/todoist/scripts/todoist.py tasks create `
  --content "Enviar relatório" --due-string "amanhã às 10h"
python skills/todoist/scripts/todoist.py tasks close --id ID
```

A skill usa a API unificada v1 e cobre tarefas, projetos, seções e etiquetas.
Alterações exigem autorização explícita na conversa. Exclusões de tarefas, seções e
projetos podem remover conteúdo descendente; arquivar deve ser preferido quando
atender ao pedido.

## Notion

```powershell
python skills/notion/scripts/notion.py pages search --query "Reunião"
python skills/notion/scripts/notion.py pages get --id ID
python skills/notion/scripts/notion.py pages find-content `
  --query "termo interno" --max-pages 10
python skills/notion/scripts/notion.py pages create `
  --parent-page-id ID --title "Nova nota" --markdown-file data/work/nova-nota.md
```

A API busca títulos, não o texto completo das páginas. `find-content` implementa uma
varredura controlada: seleciona páginas acessíveis, lê o Markdown e devolve somente
trechos correspondentes. Criações e substituições recebem conteúdo por arquivos
UTF-8 privados; alterações pontuais usam uma lista JSON de `old_str` e `new_str`.
Envio à lixeira é reversível, e exclusão permanente não faz parte da skill.

## Gmail

```powershell
python scripts/google_accounts.py list
python skills/gmail/scripts/gmail.py --profile pessoal doctor
python skills/gmail/scripts/gmail.py --profile pessoal messages list `
  --query "is:unread newer_than:7d"
python skills/gmail/scripts/gmail.py --profile pessoal messages show `
  --id ID --format full
python skills/gmail/scripts/gmail.py --profile pessoal drafts create `
  --message-file data/work/resposta.eml
```

A skill cobre mensagens, threads, marcadores e rascunhos. Mensagens novas são
preparadas primeiro como rascunho; o envio é uma operação separada que exige pedido
explícito. Modificação de marcadores e lixeira também alteram o Gmail. A exclusão
permanente não é oferecida.

Cada execução troca o refresh token armazenado no KeePassXC por um access token
temporário. O refresh token não é devolvido ao agente e o access token é descartado ao
final do processo.

## CPFL

```powershell
python skills/cpfl/scripts/cpfl.py --profile pessoal doctor
python skills/cpfl/scripts/cpfl.py --profile pessoal latest
python skills/cpfl/scripts/cpfl.py --profile pessoal payment-data
```

A skill pesquisa somente mensagens autenticadas de `contadigital@cpfl.com.br`, valida
o link individual contra uma allowlist fechada e obtém os quatro primeiros dígitos do
CPF diretamente do cofre. `payment-data` salva PIX e linha digitável em
`data/work/cpfl/`; esses valores não aparecem no JSON devolvido ao agente.

O PDF usa reCAPTCHA e requer navegador. A automação deve seguir o fluxo de CAPTCHA do
navegador e nunca tentar contornar o controle. Obter dados da conta não autoriza
pagamento.

## Google Calendar

```powershell
python skills/calendar/scripts/calendar.py --profile pessoal calendars list
python skills/calendar/scripts/calendar.py --profile pessoal events list `
  --time-min "2026-08-01T00:00:00-03:00" `
  --time-max "2026-08-08T00:00:00-03:00"
python skills/calendar/scripts/calendar.py --profile pessoal freebusy `
  --time-min "2026-08-01T09:00:00-03:00" `
  --time-max "2026-08-01T18:00:00-03:00" --calendar-id primary
```

Criação, atualização e cancelamento de eventos exigem autorização explícita. O envio
de notificações a participantes fica desativado por padrão. A skill também cobre
instâncias recorrentes, regras `RRULE` e lembretes personalizados.

## Google Drive

```powershell
python skills/drive/scripts/drive.py --profile pessoal files list `
  --query "name contains 'Relatório' and trashed = false"
python skills/drive/scripts/drive.py --profile pessoal files show --id ID
python skills/drive/scripts/drive.py --profile pessoal files upload `
  --source data/work/relatorio.pdf --parent-id ID --dry-run
```

A skill pesquisa, baixa, exporta, envia, substitui, copia, renomeia, move e coloca
itens na lixeira, além de listar drives compartilhados. Exclusão permanente não é
oferecida. Compartilhamentos, mudanças de função e notificações dependem de
autorização explícita.

## Google Contacts

```powershell
python skills/contacts/scripts/contacts.py --profile pessoal contacts search `
  --query "Maria"
python skills/contacts/scripts/contacts.py --profile pessoal contacts show `
  --resource-name people/ID
python skills/contacts/scripts/contacts.py --profile pessoal contacts create `
  --name "Maria Silva" --email "maria@example.com" --dry-run
```

Atualizações preservam os metadados de concorrência devolvidos pela People API. A
skill também administra grupos e membros sem excluir os contatos ao apagar um grupo.
Criação, alteração e exclusão exigem autorização explícita; a exclusão de um contato
é permanente.

## Interface Telegram

A interface em `interfaces/telegram/` conecta uma conversa particular autorizada a
sessões do Codex CLI. Ela opera por long polling, mantém caixas isoladas em
`data/telegram/jobs/` e persiste fila, autorizações, referências, threads, turnos e
artefatos em um SQLite local da máquina. Respostas podem referenciar mensagens e enviar
mídias nativas validadas.

O gateway executa o CLI com um `CODEX_HOME` próprio, por padrão em
`%LOCALAPPDATA%\Coworker\instances\<instance_id>\codex`. Autenticação, configuração, sessões e logs da interface
ficam separados do Codex Desktop. O perfil inicial permite ler o projeto e escrever
somente em `data/`; acesso de rede e regras para os scripts oficiais são configurados
explicitamente pela interface.

O instalador seleciona `app-server` para eventos estruturados e cancelamento por turno,
mantendo `exec` como fallback configurável no TOML privado. Consulte o README da
interface para formatos, processadores e recuperação de uploads ambíguos.

```powershell
python interfaces/telegram/gateway.py init
python interfaces/telegram/gateway.py doctor
python interfaces/telegram/gateway.py pairing begin
python interfaces/telegram/gateway.py run
```

O primeiro usuário somente se torna a pessoa proprietária depois de validar um PIN
temporário no Telegram e receber aprovação local. IDs numéricos de usuário e conversa
são a fonte de autorização; nomes e usernames são apenas referências visuais. Consulte
`interfaces/telegram/README.md` para o fluxo completo.

Depois da vinculação, `/secret NomeDoServico` inicia uma captura de uso único para
senhas e tokens. A mensagem seguinte não é enviada ao Codex: o gateway a grava
diretamente no KeePassXC, mantém apenas `[Censurado por segurança]` no estado local e
tenta excluir o original do Telegram.

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
