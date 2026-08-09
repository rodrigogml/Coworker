# TOTP

Use esta skill para cadastrar, interpretar e consultar tokens TOTP de autenticação
de dois fatores.

As credenciais ficam em entradas dedicadas do grupo `TOTP` no KeePassXC. A chave
permanece no campo `Password`; issuer, conta, algoritmo, dígitos e período ficam em
atributos protegidos. Nunca revele chaves, QR Codes ou códigos em memória persistente,
logs ou mensagens desnecessárias.

Aceite URIs `otpauth://totp` e chaves Base32. Para chave sem metadados, pergunte o
sistema/issuer e a conta antes de salvar. Para consulta, confirme correspondências
ambíguas e sempre identifique issuer e conta junto do código.

Quando houver imagem, use o parser local da skill. No Telegram, prefira `/totp` para
que o gateway processe a imagem e a resposta sem encaminhá-la ao Codex.
