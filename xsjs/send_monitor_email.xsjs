/**
 * send_monitor_email.xsjs
 *
 * Endpoint XSJS para enviar el correo diario del Monitor POS vs HANA.
 * Lo llama scripts/daily_email.py desde la VM del dashboard.
 *
 * El HTML y el gráfico se generan en Python; este servicio solo arma el
 * mail con $.net.Mail (el relay SMTP lo configura HANA) y lo envía.
 *
 * POST JSON:
 * {
 *   "subject": "...",
 *   "sender": "errores.ncr@easy.com.ar",
 *   "to": ["dest1@easy.com.ar"],
 *   "cc": ["dest2@easy.com.ar"],
 *   "html": "<html>...<img src=\"cid:monitor_resumen\">...</html>",
 *   "text": "alternativa en texto plano",
 *   "inlineImages": [
 *       {
 *           "contentId": "monitor_resumen",
 *           "contentType": "image/png",
 *           "fileName": "resumen_tiendas.png",
 *           "dataBase64": "..."
 *       }
 *   ]
 * }
 *
 * GET: responde un health check y permite obtener el token CSRF del XS.
 *
 * El paquete XS que contiene este archivo debe exigir Basic Auth en su
 * .xsaccess. No se guardan credenciales en este código.
 */

var REMITENTE_PERMITIDO = "errores.ncr@easy.com.ar";
var MAX_CUERPO = 3 * 1024 * 1024;
var MAX_HTML = 1024 * 1024;
var MAX_IMAGEN_B64 = 2 * 1024 * 1024;
var MAX_DESTINATARIOS = 50;

function responder(estado, objeto) {
    $.response.status = estado;
    $.response.contentType = "application/json; charset=utf-8";
    $.response.setBody(JSON.stringify(objeto));
}

function direccionDe(valor) {
    if (!valor) {
        return "";
    }
    if (typeof valor === "string") {
        return valor.trim();
    }
    if (typeof valor === "object" && valor.address) {
        return String(valor.address).trim();
    }
    return "";
}

function normalizarDestinatarios(valor) {
    var salida = [];
    if (!valor) {
        return salida;
    }
    if (typeof valor === "string") {
        valor = [valor];
    }
    if (!Array.isArray(valor)) {
        return salida;
    }
    for (var i = 0; i < valor.length; i++) {
        var direccion = direccionDe(valor[i]);
        if (direccion) {
            salida.push({ address: direccion });
        }
    }
    return salida;
}

function main() {
    if ($.request.method === $.net.http.GET) {
        responder($.net.http.OK, {
            ok: true,
            servicio: "send_monitor_email"
        });
        return;
    }

    if ($.request.method !== $.net.http.POST) {
        responder(405, {
            ok: false,
            error: "Método no permitido. Usar POST."
        });
        return;
    }

    var cuerpoTexto;
    try {
        if (!$.request.body) {
            throw new Error("El request no tiene body");
        }
        cuerpoTexto = $.request.body.asString();
    } catch (e) {
        responder(400, {
            ok: false,
            error: "No se pudo leer el body: " + e.toString()
        });
        return;
    }

    if (!cuerpoTexto || cuerpoTexto.length > MAX_CUERPO) {
        responder(413, {
            ok: false,
            error: "Body vacío o demasiado grande"
        });
        return;
    }

    var cuerpo;
    try {
        cuerpo = JSON.parse(cuerpoTexto);
    } catch (e) {
        responder(400, {
            ok: false,
            error: "JSON inválido: " + e.toString()
        });
        return;
    }

    var asunto = String(cuerpo.subject || "").trim();
    var html = String(cuerpo.html || "");
    var texto = String(cuerpo.text || "");
    var remitente = direccionDe(cuerpo.sender) || REMITENTE_PERMITIDO;

    if (!asunto) {
        responder(400, { ok: false, error: "Falta subject" });
        return;
    }
    if (!html) {
        responder(400, { ok: false, error: "Falta html" });
        return;
    }
    if (html.length > MAX_HTML) {
        responder(413, { ok: false, error: "El HTML es demasiado grande" });
        return;
    }
    if (remitente !== REMITENTE_PERMITIDO) {
        responder(400, {
            ok: false,
            error: "Remitente no permitido: " + remitente
        });
        return;
    }

    var destinatarios = normalizarDestinatarios(cuerpo.to);
    var copias = normalizarDestinatarios(cuerpo.cc);
    if (destinatarios.length === 0) {
        responder(400, { ok: false, error: "Falta al menos un destinatario" });
        return;
    }
    if (destinatarios.length > MAX_DESTINATARIOS ||
        copias.length > MAX_DESTINATARIOS) {
        responder(400, {
            ok: false,
            error: "Demasiados destinatarios (máximo " + MAX_DESTINATARIOS + ")"
        });
        return;
    }

    var imagenes = cuerpo.inlineImages || [];
    if (!Array.isArray(imagenes)) {
        responder(400, {
            ok: false,
            error: "inlineImages debe ser un array"
        });
        return;
    }

    var mail;
    try {
        mail = new $.net.Mail();
        mail.subject = asunto;
        mail.subjectEncoding = "UTF-8";
        mail.sender = {
            name: "Monitor POS vs HANA",
            address: remitente
        };

        mail.to = destinatarios;
        if (copias.length > 0) {
            mail.cc = copias;
        }

        // Las imágenes inline van antes que la parte de texto.
        for (var k = 0; k < imagenes.length; k++) {
            var imagen = imagenes[k] || {};
            var contentId = String(imagen.contentId || "").trim();
            var contentType = String(imagen.contentType || "").trim();
            var dataBase64 = String(imagen.dataBase64 || "");

            if (!/^[A-Za-z0-9_-]+$/.test(contentId)) {
                throw new Error("contentId inválido en inlineImages[" + k + "]");
            }
            if (contentType !== "image/png") {
                throw new Error(
                    "contentType no permitido en inlineImages[" + k + "]: " +
                    contentType
                );
            }
            if (!dataBase64 || dataBase64.length > MAX_IMAGEN_B64) {
                throw new Error(
                    "Imagen vacía o demasiado grande en inlineImages[" + k + "]"
                );
            }

            var datos = $.util.codec.decodeBase64(dataBase64);
            mail.parts.push(new $.net.Mail.Part({
                type: $.net.Mail.Part.TYPE_INLINE,
                data: datos,
                contentType: "image/png",
                contentId: contentId,
                fileName: String(imagen.fileName || (contentId + ".png")),
                fileNameEncoding: "UTF-8"
            }));
        }

        var parteTexto = new $.net.Mail.Part({
            type: $.net.Mail.Part.TYPE_TEXT,
            text: html,
            contentType: "text/html",
            encoding: "UTF-8"
        });
        if (texto) {
            parteTexto.alternative = texto;
            parteTexto.alternativeContentType = "text/plain";
        }
        mail.parts.push(parteTexto);

        $.trace.info(
            "send_monitor_email: antes de mail.send - " +
            new Date().toISOString()
        );
        var resultado = mail.send();
        $.trace.info(
            "send_monitor_email: después de mail.send - " +
            new Date().toISOString()
        );

        responder($.net.http.OK, {
            ok: true,
            messageId: resultado && resultado.messageId ?
                String(resultado.messageId) : "",
            finalReply: resultado && resultado.finalReply ?
                String(resultado.finalReply) : ""
        });
    } catch (e) {
        $.trace.error("send_monitor_email: error: " + e.toString());
        responder(500, {
            ok: false,
            error: e.toString()
        });
    }
}

main();
