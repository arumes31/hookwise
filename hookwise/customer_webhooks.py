"""Authenticated customer webhook management."""

from typing import Any
from urllib.parse import urlsplit

from flask import flash, redirect, render_template, request, url_for

from .extensions import db
from .models import CustomerWebhook, CustomerWebhookDelivery, GlobalMapping, WebhookConfig
from .rbac import requires
from .routes import main_bp
from .services.customer_webhooks import (
    TERMINAL_STATUSES,
    numeric_score,
    set_destination,
    validate_score_path,
    validate_target_url,
)
from .utils import auth_required, decrypt_string, log_audit


@main_bp.get("/customer-webhooks")
@auth_required
@requires("endpoint:read")
def customer_webhooks() -> Any:
    hooks = CustomerWebhook.query.order_by(CustomerWebhook.created_at.desc()).all()
    hosts = {}
    for hook in hooks:
        try:
            hosts[hook.id] = urlsplit(decrypt_string(hook.target_url)).hostname
        except ValueError:
            hosts[hook.id] = "Ziel nicht lesbar"
    return render_template("customer_webhooks.html", hooks=hooks, hosts=hosts)


def _render_form(hook: CustomerWebhook | None = None, values: Any = None, error: str | None = None) -> Any:
    configs = WebhookConfig.query.filter(WebhookConfig.archived_at.is_(None)).order_by(WebhookConfig.name).all()
    companies = db.session.query(GlobalMapping.company_id, GlobalMapping.description).distinct().all()
    return render_template(
        "customer_webhook_form.html", hook=hook, values=values, configs=configs, companies=companies, error=error
    )


def _save(hook: CustomerWebhook, creating: bool) -> Any:
    form = request.form
    try:
        name = form.get("name", "").strip()
        comment = form.get("comment", "").strip()
        company_id = form.get("company_id", "").strip()
        config = db.session.get(WebhookConfig, form.get("config_id", ""))
        path = form.get("score_path", "").strip()
        threshold = numeric_score(form.get("minimum_score"))
        url = form.get("target_url", "").strip()
        if not name or len(name) > 100 or not company_id or len(company_id) > 50:
            raise ValueError("Name (max. 100 Zeichen) und CW Company ID (max. 50 Zeichen) sind erforderlich.")
        if len(comment) > 2000:
            raise ValueError("Der Kommentar darf höchstens 2000 Zeichen enthalten.")
        if config is None or config.archived_at:
            raise ValueError("Bitte einen gültigen Quell-Endpoint auswählen.")
        if threshold is None:
            raise ValueError("Der Mindestscore muss eine endliche Zahl ab 0 sein.")
        validate_score_path(path)
        if url or creating:
            validate_target_url(url)
        # Pending deliveries retain the original customer boundary. Changing a
        # subscription's customer/source would silently reroute those alarms.
        if not creating and (hook.company_id != company_id or hook.config_id != config.id):
            raise ValueError("Kunde und Quelle sind fest zugeordnet. Dafür bitte einen neuen Webhook anlegen.")
        for field in ("bearer_token", "signing_secret"):
            value = form.get(field, "").strip()
            if len(value) > 2048 or "\r" in value or "\n" in value:
                raise ValueError("Zugangsdaten dürfen höchstens 2048 Zeichen und keine Zeilenumbrüche enthalten.")
        hook.name, hook.company_id, hook.config_id = name, company_id, config.id
        hook.comment = comment or None
        hook.score_path, hook.minimum_score = path, threshold
        hook.is_enabled = form.get("is_enabled") == "on"
        credentials: dict[str, str | None] = {}
        for field in ("bearer_token", "signing_secret"):
            value = form.get(field, "").strip()
            if value:
                credentials[field] = value
            elif form.get(f"clear_{field}") == "on":
                credentials[field] = None
        set_destination(hook, url or decrypt_string(hook.target_url), credentials, drop_replayed=False)
        db.session.add(hook)
        db.session.flush()
        log_audit(
            "customer_webhook_create" if creating else "customer_webhook_update",
            hook.config_id,
            f"Customer webhook {hook.id}; company {hook.company_id}",
            commit=False,
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        # Never reflect credentials or the target URL back into the form.
        safe_values = {
            key: form.get(key, "")
            for key in (
                "name",
                "comment",
                "company_id",
                "config_id",
                "score_path",
                "minimum_score",
                "is_enabled",
            )
        }
        return _render_form(None if creating else hook, safe_values, str(exc)), 400
    flash("Forwarding connector gespeichert.", "success")
    return redirect(url_for("main.customer_webhooks"))


@main_bp.route("/customer-webhooks/new", methods=["GET", "POST"])
@auth_required
@requires("endpoint:write")
def new_customer_webhook() -> Any:
    if request.method == "POST":
        return _save(CustomerWebhook(), True)
    return _render_form()


@main_bp.route("/customer-webhooks/<webhook_id>/edit", methods=["GET", "POST"])
@auth_required
@requires("endpoint:write")
def edit_customer_webhook(webhook_id: str) -> Any:
    hook = CustomerWebhook.query.filter_by(id=webhook_id).with_for_update().first_or_404()
    if request.method == "POST":
        return _save(hook, False)
    return _render_form(hook)


@main_bp.post("/customer-webhooks/<webhook_id>/toggle")
@auth_required
@requires("endpoint:write")
def toggle_customer_webhook(webhook_id: str) -> Any:
    hook = db.get_or_404(CustomerWebhook, webhook_id)
    hook.is_enabled = not hook.is_enabled
    log_audit(
        "customer_webhook_toggle",
        hook.config_id,
        f"Customer webhook {hook.id}; enabled={hook.is_enabled}",
        commit=False,
    )
    db.session.commit()
    return redirect(url_for("main.customer_webhooks"))


@main_bp.get("/customer-webhooks/<webhook_id>/deliveries")
@auth_required
@requires("history:read")
def customer_webhook_deliveries(webhook_id: str) -> Any:
    hook = db.get_or_404(CustomerWebhook, webhook_id)
    page = request.args.get("page", 1, type=int)
    deliveries = (
        CustomerWebhookDelivery.query.filter_by(webhook_id=hook.id)
        .order_by(
            CustomerWebhookDelivery.created_at.desc(),
        )
        .paginate(page=max(1, page), per_page=50, error_out=False)
    )
    return render_template("customer_webhook_deliveries.html", hook=hook, deliveries=deliveries)


@main_bp.post("/customer-webhooks/<webhook_id>/deliveries/clear")
@auth_required
@requires("history:delete")
def clear_customer_webhook_history(webhook_id: str) -> Any:
    hook = db.get_or_404(CustomerWebhook, webhook_id)
    count = CustomerWebhookDelivery.query.filter(
        CustomerWebhookDelivery.webhook_id == hook.id,
        CustomerWebhookDelivery.status.in_(TERMINAL_STATUSES),
    ).delete(synchronize_session=False)
    log_audit("forwarding_history_clear", hook.config_id, f"Cleared {count} forwarding deliveries", commit=False)
    db.session.commit()
    flash("Forwarding history cleared. Pending deliveries were kept.", "success")
    return redirect(url_for("main.customer_webhook_deliveries", webhook_id=hook.id))
