# Política de segurança

## Versões atendidas

Enquanto a BOTina estiver em estágio experimental, somente a versão mais recente da
branch `main` recebe correções de segurança.

## Como relatar uma vulnerabilidade

Não publique vulnerabilidades, credenciais, caminhos privados, dados pessoais ou
procedimentos de exploração em issues, discussions ou pull requests.

Use a opção **Report a vulnerability** na aba **Security** do repositório para enviar
um relato privado. Se essa opção não estiver disponível, contate o proprietário pelo
perfil do GitHub para combinar um canal privado antes de compartilhar detalhes.

Inclua somente:

- descrição do comportamento;
- impacto provável;
- passos mínimos de reprodução com dados fictícios;
- versão, sistema operacional e componente afetado;
- sugestão de correção, quando houver.

Nunca inclua senhas, tokens, chaves privadas ou conteúdo real do cofre. Se uma
credencial verdadeira tiver sido exposta, revogue ou rotacione-a imediatamente; apagar
o texto de uma mensagem ou commit não torna a credencial segura novamente.

## Escopo prioritário

Recebem prioridade vulnerabilidades que possam:

- revelar segredos ao agente, terminal, logs ou processos;
- permitir leitura ou escrita fora dos caminhos autorizados;
- executar comandos diferentes dos parâmetros apresentados;
- modificar sistemas externos sem autorização;
- incluir arquivos de `data/` ou outras informações privadas no Git;
- contornar confirmações exigidas para ações destrutivas.

Relatos sem impacto de segurança reproduzível podem ser tratados como bugs comuns.
