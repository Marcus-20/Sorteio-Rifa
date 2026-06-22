from flask import Flask, jsonify, request, render_template
import mysql.connector
import mercadopago
import re
import random
import os  # Adicionado para ler as variáveis de ambiente da nuvem
import psycopg2  # Adaptador para o banco de dados gratuito do Render
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# === CONFIGURAÇÕES ===
VALOR_NUMERO = 5.00
# Token de Acesso configurado (Lembre-se de usar TEST-... caso precise homologar)
MERCADOPAGO_TOKEN = "APP_USR-3437113290587947-062121-0088376aadfb8e3c05571a6e2dd9901e-3488609849"

def conectar_banco():
    # Se estiver rodando na nuvem (Render), ele pega a URL automaticamente
    url_banco_nuvem = os.environ.get("DATABASE_URL")
    
    if url_banco_nuvem:
        # Conexão com o banco gratuito PostgreSQL no Render
        return psycopg2.connect(url_banco_nuvem)
    else:
        # Conexão padrão com o seu MySQL local (Computador)
        return mysql.connector.connect(
            host="127.0.0.1",
            port=3306,
            user="root",       
            password="03022007",     
            database="sistema_rifa"
        )

# Rota principal para carregar a página HTML
@app.route('/')
def index():
    return render_template('index.html')

# 1. API para listar todos os 100 números e os seus estados
@app.route('/api/numeros', methods=['GET'])
def listar_numeros():
    try:
        conexao = conectar_banco()
        
        # Ajuste inteligente para lidar com os formatos de dicionário de ambos os bancos
        if hasattr(conexao, 'cursor_factory'):
            cursor = conexao.cursor(cursor_factory=RealDictCursor)
        else:
            cursor = conexao.cursor(dictionary=True)
            
        cursor.execute("SELECT numero, status, nome_comprador, telefone FROM sorteio_liquidificador")
        numeros = cursor.fetchall()
        cursor.close()
        conexao.close()
        return jsonify(numeros)
    except Exception as e:
        return jsonify({"erro": f"Erro ao acessar o banco de dados: {str(e)}"}), 500

# 2. API para receber a reserva dos números e criar o PIX no Mercado Pago
@app.route('/api/reservar', methods=['POST'])
def reservar_numeros():
    dados = request.json
    nome = dados.get('nome', 'Comprador Anonimo')
    telefone_bruto = dados.get('telefone', '')
    numeros_escolhidos = dados.get('numeros') 

    if not numeros_escolhidos:
        return jsonify({"erro": "Nenhum número selecionado"}), 400

    try:
        conexao = conectar_banco()
        cursor = conexao.cursor()
        
        # Verifica se algum dos números escolhidos já não está disponível
        format_strings = ','.join(['%s'] * len(numeros_escolhidos))
        cursor.execute(f"SELECT numero FROM sorteio_liquidificador WHERE numero IN ({format_strings}) AND status != 'Disponível'", tuple(numeros_escolhidos))
        ocupados = cursor.fetchall()
        
        if ocupados:
            cursor.close()
            conexao.close()
            return jsonify({"erro": f"Os números {[o[0] for o in ocupados]} já foram reservados ou pagos."}), 400

        # Trata o telefone para manter apenas dígitos numéricos
        telefone_limpo = re.sub(r'\D', '', str(telefone_bruto))
        if len(telefone_limpo) < 10:
            return jsonify({"erro": "Por favor, digite um telefone válido com DDD (apenas números)."}), 400
        
        ddd = telefone_limpo[:2]
        numero_tel = telefone_limpo[2:]

        # Calcula o valor total com base na quantidade de números escolhidos
        valor_total = len(numeros_escolhidos) * VALOR_NUMERO

        # Integração com o SDK do Mercado Pago
        mp = mercadopago.SDK(MERCADOPAGO_TOKEN)
        
        ref_id = f"sorteio-{'_'.join(map(str, numeros_escolhidos))}"
        
        # Cria um e-mail dinâmico baseado no telefone para evitar rejeição por e-mail fixo/falso repetido
        email_comprador = f"cliente_{telefone_limpo}@gmail.com"

        payment_data = {
            "transaction_amount": float(valor_total),
            "description": f"Rifa Liquidificador - Nr: {numeros_escolhidos}",
            "payment_method_id": "pix",
            "payer": {
                "email": email_comprador, 
                "first_name": nome,
                "phone": {
                    "area_code": ddd,
                    "number": numero_tel
                },
                "identification": {
                    "type": "CPF",
                    "number": "13620758417" # CPF estruturalmente válido para a API de produção
                }
            },
            "external_reference": ref_id
        }

        pagamento_resposta = mp.payment().create(payment_data)
        pagamento = pagamento_resposta["response"]

        # Se o Mercado Pago gerou o PIX com sucesso
        if "point_of_interaction" in pagamento:
            qr_code_copia_cola = pagamento["point_of_interaction"]["transaction_data"]["qr_code"]
            qr_code_base64 = pagamento["point_of_interaction"]["transaction_data"]["qr_code_base64"]
            id_pagamento_mp = pagamento["id"]

            for num in numeros_escolhidos:
                cursor.execute(
                    "UPDATE sorteio_liquidificador SET nome_comprador=%s, telefone=%s, status='Reservado' WHERE numero=%s",
                    (nome, telefone_limpo, num)
                )
            conexao.commit()
            cursor.close()
            conexao.close()

            return jsonify({
                "status": "Reservado",
                "total": valor_total,
                "id_pagamento": id_pagamento_mp,
                "pix_copia_cola": qr_code_copia_cola,
                "pix_image": qr_code_base64
            })
        else:
            cursor.close()
            conexao.close()
            print("--- ERRO DETALHADO REJEITADO PELO MERCADO PAGO ---")
            print(pagamento)
            print("-------------------------------------------------")
            
            mensagem_erro = pagamento.get("message", "Verifique os parâmetros enviados.")
            return jsonify({"erro": f"Mercado Pago recusou: {mensagem_erro}"}), 400

    except Exception as e:
        return jsonify({"erro": f"Erro interno no servidor: {str(e)}"}), 500

# 3. WEBHOOK AUTOMÁTICO: O Mercado Pago avisa esta rota quando detectar o pagamento
@app.route('/webhook-pagamento', methods=['POST'])
def webhook_mercado_pago():
    id_recurso = request.args.get('data.id')
    tipo_recurso = request.args.get('type')

    if not id_recurso and request.json:
        id_recurso = request.json.get('data', {}).get('id')
        tipo_recurso = request.json.get('type')

    if tipo_recurso == 'payment' and id_recurso:
        try:
            mp = mercadopago.SDK(MERCADOPAGO_TOKEN)
            dados_pagamento = mp.payment().get(id_recurso)["response"]
            
            status_pagamento = dados_pagamento.get("status") 
            referencia_externa = dados_pagamento.get("external_reference") 

            if status_pagamento == "approved" and referencia_externa:
                partes = referencia_externa.split('-')
                numeros_pagos = [int(n) for n in partes[1].split('_')]

                conexao = conectar_banco()
                cursor = conexao.cursor()
                
                format_strings = ','.join(['%s'] * len(numeros_pagos))
                cursor.execute(f"UPDATE sorteio_liquidificador SET status='Pago' WHERE numero IN ({format_strings})", tuple(numeros_pagos))
                conexao.commit()
                
                cursor.close()
                conexao.close()
                print(f"🎉 SUCESSO: Pagamento {id_recurso} aprovado! Números confirmados: {numeros_pagos}")
        except Exception as e:
            print(f"⚠️ Erro ao processar o webhook para o pagamento {id_recurso}: {str(e)}")

    return jsonify({"status": "recebido"}), 200

# === ROTAS DE ADMINISTRAÇÃO ===

# Rota para abrir o painel de administração no navegador
@app.route('/admin')
def admin_painel():
    return render_template('admin.html')

# API para realizar o sorteio aleatório entre os números PAGOS
@app.route('/api/admin/sortear', methods=['POST'])
def realizar_sorteio():
    try:
        conexao = conectar_banco()
        
        if hasattr(conexao, 'cursor_factory'):
            cursor = conexao.cursor(cursor_factory=RealDictCursor)
        else:
            cursor = conexao.cursor(dictionary=True)
        
        # Filtra apenas os participantes com números pagos
        cursor.execute("SELECT numero, nome_comprador, telefone FROM sorteio_liquidificador WHERE status = 'Pago'")
        numeros_pagos = cursor.fetchall()
        
        cursor.close()
        conexao.close()

        if not numeros_pagos:
            return jsonify({"erro": "Nenhum número foi pago ainda. O sorteio não pode ser realizado!"}), 400

        # Sorteia um dicionário contendo os dados do felizardo
        ganhador = random.choice(numeros_pagos)
        
        return jsonify({
            "sucesso": True,
            "numero": str(ganhador['numero']).zfill(2),
            "nome": ganhador['nome_comprador'],
            "telefone": ganhador['telefone']
        })
    except Exception as e:
        return jsonify({"erro": f"Erro interno ao sortear: {str(e)}"}), 500

# API exclusiva do Admin para zerar a rifa inteira e começar de novo
@app.route('/api/admin/reset', methods=['POST'])
def resetar_rifa():
    try:
        conexao = conectar_banco()
        cursor = conexao.cursor()
        cursor.execute("UPDATE sorteio_liquidificador SET nome_comprador=NULL, telefone=NULL, status='Disponível'")
        conexao.commit()
        cursor.close()
        conexao.close()
        return jsonify({"sucesso": True, "mensagem": "Rifa reiniciada com sucesso!"})
    except Exception as e:
        return jsonify({"erro": f"Erro ao resetar: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)