---
name: cpfl
description: Localizar contas digitais da CPFL no Gmail, validar links oficiais, consultar metadados da fatura e obter PIX ou linha digitável em arquivo privado. Usar quando a tarefa mencionar conta de energia, fatura, unidade consumidora, PIX, código de barras ou PDF da CPFL.
---

# Consultar contas da CPFL

Usar `python skills/cpfl/scripts/cpfl.py`. Selecionar a configuração com
`--profile`; nunca fornecer CPF, link individual ou código de pagamento em argumentos.

## Executar o fluxo

1. Executar `doctor` para validar Gmail, pessoa titular e configuração.
2. Usar `latest` para localizar a mensagem autenticada mais recente.
3. Usar `payment-data` para obter PIX e linha digitável em arquivo privado.
4. Para PDF, usar o navegador conforme a seção específica.

```powershell
python skills/cpfl/scripts/cpfl.py --profile pessoal doctor
python skills/cpfl/scripts/cpfl.py --profile pessoal latest
python skills/cpfl/scripts/cpfl.py --profile pessoal payment-data
python skills/cpfl/scripts/cpfl.py --profile pessoal payment-data --message-id ID
```

`payment-data` grava valores somente em `data/work/cpfl/` e devolve metadados de
validação. Não abrir nem reproduzir o arquivo quando a tarefa pedir apenas conferência.

## Baixar o PDF

O botão **Veja sua conta** usa reCAPTCHA. Não tentar contorná-lo por HTTP.

1. Obter a mensagem com `latest` e recuperar internamente seu link oficial.
2. Abrir o link no navegador controlado.
3. Ler `CPF` internamente por `scripts/vault_entities.py` e preencher somente os quatro
   primeiros dígitos.
4. Antes de resolver o reCAPTCHA, seguir a política de confirmação do navegador.
5. Se houver desafio visual, deixar a aba aberta para a pessoa usuária concluí-lo.
6. Capturar o evento de download e salvar o PDF dentro de `data/`.

Depois do download, usar a skill de PDF para extrair e validar os campos solicitados.

## Respeitar contratos

- Aceitar links somente de mensagens autenticadas de `contadigital@cpfl.com.br`.
- Aceitar somente HTTPS no host e caminho definidos na configuração.
- Obter CPF exclusivamente da propriedade protegida da pessoa titular.
- Nunca imprimir link individual, CPF, PIX, linha digitável, `VIEWSTATE` ou cookies.
- Não armazenar códigos de pagamento fora de `data/`.
- Obter dados de pagamento não autoriza realizar pagamento.
- Não repetir automaticamente um POST após timeout ou resposta ambígua.
