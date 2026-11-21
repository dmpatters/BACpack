"""Web server settings: the listen address and the HTTPS certificate.
All routes are plain `def`: they save under SQLite's
write lock and write files. No response ever holds key material; only the
public certificate can be downloaded.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ... import lifecycle, timeutil, tls_certs, web_server
from ...db import lock_both_files
from ...tls_certs import CertInfo, CertProblem
from ..auth_middleware import client_ip
from ..deps import get_data_dir, get_session

router = APIRouter()


class ListenAddressBody(BaseModel):
    listen_address: str


class CertificateBody(BaseModel):
    certificate_pem: str
    key_pem: str


def _certificate_dict(info: CertInfo) -> dict:
    return {
        "fingerprint_sha256": info.fingerprint_sha256,
        "subject": info.subject,
        "issuer": info.issuer,
        "not_before": timeutil.iso_utc(info.not_before),
        "not_after": timeutil.iso_utc(info.not_after),
        "sans": list(info.sans),
        "source": "self-signed" if info.generated_by_bacpack else "own",
        "expired": info.not_after < timeutil.utc_now(),
    }


def _payload(session: Session, data_dir: Path) -> dict:
    listen_address = web_server.get_listen_address(session)
    status = web_server.certificate_status(data_dir)
    running = lifecycle.running_web()
    restart_pending = running is not None and (
        running.address != listen_address
        or (status.info is not None and running.fingerprint != status.info.fingerprint_sha256)
    )
    return {
        "listen_address": listen_address,
        "running": (
            {"address": running.address, "port": running.port, "fingerprint": running.fingerprint}
            if running
            else None
        ),
        "certificate": _certificate_dict(status.info) if status.info else None,
        "certificate_problem": status.problem,
        "restart_pending": restart_pending,
    }


@router.get("/settings/web")
def get_web_settings(session: Session = Depends(get_session), data_dir: Path = Depends(get_data_dir)):
    return _payload(session, data_dir)


@router.put("/settings/web")
def put_web_settings(
    body: ListenAddressBody,
    request: Request,
    session: Session = Depends(get_session),
    data_dir: Path = Depends(get_data_dir),
):
    lock_both_files(session)
    try:
        web_server.set_listen_address(session, body.listen_address, client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _payload(session, data_dir)


@router.get("/settings/web/certificate.pem")
def download_certificate(data_dir: Path = Depends(get_data_dir)):
    try:
        pem = tls_certs.read_certificate_pem(data_dir)
    except OSError:
        raise HTTPException(status_code=404, detail="no certificate has been made yet")
    except CertProblem as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return Response(
        pem,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="bacpack-certificate.pem"'},
    )


@router.post("/settings/web/certificate/self-signed")
def use_self_signed(
    request: Request, session: Session = Depends(get_session), data_dir: Path = Depends(get_data_dir)
):
    try:
        web_server.make_self_signed(data_dir, client_ip(request))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot write the certificate files: {exc.strerror or exc}")
    return _payload(session, data_dir)


@router.post("/settings/web/certificate")
def upload_certificate(
    body: CertificateBody,
    request: Request,
    session: Session = Depends(get_session),
    data_dir: Path = Depends(get_data_dir),
):
    cert_pem, key_pem = body.certificate_pem.encode(), body.key_pem.encode()
    try:
        # The size limit is checked inside check_pair, before any parsing; a
        # refusal never echoes the input (see app._validation_error).
        web_server.install_own_certificate(data_dir, cert_pem, key_pem, client_ip(request))
    except CertProblem as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot write the certificate files: {exc.strerror or exc}")
    return _payload(session, data_dir)
