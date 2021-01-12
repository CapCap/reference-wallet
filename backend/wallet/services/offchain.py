# Copyright (c) The Diem Core Contributors
# SPDX-License-Identifier: Apache-2.0
import json
import logging
import time
import typing
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple, Callable, List, Dict

import context
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from diem import offchain, identifier
from diem.offchain import (
    CommandType,
    FundPullPreApprovalStatus,
    FundsPullPreApprovalCommand,
)
from diem_utils.types.currencies import DiemCurrency
from wallet import storage
from wallet.services import account, kyc
from wallet.storage import models

# noinspection PyUnresolvedReferences
from wallet.storage.funds_pull_pre_approval_command import (
    get_account_commands,
    update_command,
    FundsPullPreApprovalCommandNotFound,
    commit_command,
    get_commands_by_send_status,
    get_funds_pull_pre_approval_command,
    update_command_2,
)

from ..storage import (
    lock_for_update,
    commit_transaction,
    get_transactions_by_status,
    get_account_id_from_subaddr,
    Transaction,
)
from ..types import (
    TransactionType,
    TransactionStatus,
)

logger = logging.getLogger(__name__)


class Role(str, Enum):
    PAYEE = "payee"
    PAYER = "payer"


def save_outbound_transaction(
    sender_id: int,
    destination_address: str,
    destination_subaddress: str,
    amount: int,
    currency: DiemCurrency,
) -> Transaction:
    sender_onchain_address = context.get().config.vasp_address
    sender_subaddress = account.generate_new_subaddress(account_id=sender_id)
    return commit_transaction(
        _new_payment_command_transaction(
            offchain.PaymentCommand.init(
                identifier.encode_account(
                    sender_onchain_address, sender_subaddress, _hrp()
                ),
                _user_kyc_data(sender_id),
                identifier.encode_account(
                    destination_address, destination_subaddress, _hrp()
                ),
                amount,
                currency.value,
            ),
            TransactionStatus.OFF_CHAIN_OUTBOUND,
        )
    )


def process_inbound_command(
    request_sender_address: str, request_body_bytes: bytes
) -> (int, bytes):
    command = None
    try:
        command = _offchain_client().process_inbound_request(
            request_sender_address, request_body_bytes
        )

        if command.command_type() == CommandType.PaymentCommand:
            payment_command = typing.cast(offchain.PaymentCommand, command)
            _lock_and_save_inbound_command(payment_command)
        elif command.command_type() == CommandType.FundPullPreApprovalCommand:
            preapproval_command = typing.cast(
                offchain.FundsPullPreApprovalCommand, command
            )
            approval = preapproval_command.funds_pull_pre_approval

            # TODO add timestamp validation
            validate_expiration_timestamp(approval.scope.expiration_timestamp)

            bech32_address = approval.address

            role = get_role_by_fppa_command_status(preapproval_command)
            # TODO check if command already in db
            command_in_db = get_funds_pull_pre_approval_command(
                preapproval_command.reference_id()
            )

            if command_in_db:
                # TODO verify that address and biiler_address are equal
                if (
                    approval.address != command_in_db.address
                    or approval.biller_address != command_in_db.biller_address
                ):
                    raise ValueError("address and biller_addres values are immutable")
                # TODO if exist - verify the incoming status make since - update_command
                update_command_2(
                    preapproval_command_to_model(
                        account_id=command_in_db.account_id,
                        command=preapproval_command,
                        role=role,
                    )
                )
            else:
                # TODO if not exist - commit_command
                commit_command(
                    preapproval_command_to_model(
                        account_id=account.get_account_id_from_bech32(bech32_address),
                        command=preapproval_command,
                        role=role,
                    )
                )

            # TODO each scenario is a test case
        else:
            # TODO log?
            ...

        return _jws(command.id())
    except offchain.Error as e:
        logger.exception(e)
        return _jws(command.id() if command else None, e.obj)


def get_role_by_fppa_command_status(preapproval_command):
    if (
        preapproval_command.funds_pull_pre_approval.status is None
        or preapproval_command.funds_pull_pre_approval.status == "pending"
    ):
        return Role.PAYER
    elif (
        preapproval_command.funds_pull_pre_approval.status
        in typing.Union["valid", "rejected"]
    ):
        return Role.PAYEE


def _jws(cid: Optional[str], err: Optional[offchain.OffChainErrorObject] = None):
    code = 400 if err else 200
    resp = offchain.reply_request(cid)
    return code, offchain.jws.serialize(resp, _compliance_private_key().sign)


def _process_funds_pull_pre_approvals_requests():
    commands = get_commands_by_send_status(False)

    for command in commands:
        if command.role == Role.PAYER:
            my_address = command.address
        else:
            my_address = command.biller_address

        cmd = preapproval_model_to_command(my_address=my_address, command=command)

        _offchain_client().send_command(cmd, _compliance_private_key().sign)

        update_command(command.funds_pull_pre_approval_id, command, command.role, True)


def process_offchain_tasks() -> None:
    def send_command(txn, cmd, _) -> None:
        assert not cmd.is_inbound()
        txn.status = TransactionStatus.OFF_CHAIN_WAIT
        _offchain_client().send_command(cmd, _compliance_private_key().sign)

    def offchain_action(txn, cmd, action) -> None:
        assert cmd.is_inbound()
        if action is None:
            return
        if action == offchain.Action.EVALUATE_KYC_DATA:
            new_cmd = _evaluate_kyc_data(cmd)
            txn.command_json = offchain.to_json(new_cmd)
            txn.status = _command_transaction_status(
                new_cmd, TransactionStatus.OFF_CHAIN_OUTBOUND
            )
        else:
            # todo: handle REVIEW_KYC_DATA and CLEAR_SOFT_MATCH
            raise ValueError(f"unsupported offchain action: {action}, command: {cmd}")

    def submit_txn(txn, cmd, _) -> Transaction:
        if cmd.is_sender():
            logger.info(
                f"Submitting transaction ID:{txn.id} {txn.amount} {txn.currency}"
            )
            _offchain_client().send_command(cmd, _compliance_private_key().sign)
            rpc_txn = context.get().p2p_by_travel_rule(
                cmd.receiver_account_address(_hrp()),
                cmd.payment.action.currency,
                cmd.payment.action.amount,
                cmd.travel_rule_metadata(_hrp()),
                bytes.fromhex(cmd.payment.recipient_signature),
            )
            txn.sequence = rpc_txn.transaction.sequence_number
            txn.blockchain_version = rpc_txn.version
            txn.status = TransactionStatus.COMPLETED
            logger.info(
                f"Submitted transaction ID:{txn.id} V:{txn.blockchain_version} {txn.amount} {txn.currency}"
            )

    _process_payment_by_status(TransactionStatus.OFF_CHAIN_OUTBOUND, send_command)
    _process_payment_by_status(TransactionStatus.OFF_CHAIN_INBOUND, offchain_action)
    _process_payment_by_status(TransactionStatus.OFF_CHAIN_READY, submit_txn)
    _process_funds_pull_pre_approvals_requests()


def _process_payment_by_status(
    status: TransactionStatus,
    callback: Callable[
        [Transaction, offchain.PaymentCommand, offchain.Action], Optional[Transaction]
    ],
) -> None:
    txns = get_transactions_by_status(status)
    for txn in txns:
        cmd = _txn_payment_command(txn)
        action = cmd.follow_up_action()

        def callback_with_status_check(txn):
            if txn.status == status:
                callback(txn, cmd, action)
            return txn

        logger.info(f"lock for update: {action} {cmd}")
        try:
            lock_for_update(txn.reference_id, callback_with_status_check)
        except Exception:
            logger.exception("process offchain transaction failed")


def _evaluate_kyc_data(command: offchain.PaymentObject) -> offchain.PaymentObject:
    # todo: evaluate command.opponent_actor_obj().kyc_data
    # when pass evaluation, we send kyc data as receiver or ready for settlement as sender
    if command.is_receiver():
        return _send_kyc_data_and_receipient_signature(command)
    return command.new_command(status=offchain.Status.ready_for_settlement)


def _send_kyc_data_and_receipient_signature(
    command: offchain.PaymentCommand,
) -> offchain.PaymentCommand:
    sig_msg = command.travel_rule_metadata_signature_message(_hrp())
    user_id = get_account_id_from_subaddr(command.receiver_subaddress(_hrp()).hex())

    return command.new_command(
        recipient_signature=_compliance_private_key().sign(sig_msg).hex(),
        kyc_data=_user_kyc_data(user_id),
        status=offchain.Status.ready_for_settlement,
    )


def _lock_and_save_inbound_command(command: offchain.PaymentCommand) -> Transaction:
    def validate_and_save(txn: Optional[Transaction]) -> Transaction:
        if txn:
            prior = _txn_payment_command(txn)
            if command == prior:
                return
            command.validate(prior)
            txn.command_json = offchain.to_json(command)
            txn.status = _command_transaction_status(
                command, TransactionStatus.OFF_CHAIN_INBOUND
            )
        else:
            txn = _new_payment_command_transaction(
                command, TransactionStatus.OFF_CHAIN_INBOUND
            )
        return txn

    return lock_for_update(command.reference_id(), validate_and_save)


def _command_transaction_status(
    command: offchain.PaymentCommand, default: TransactionStatus
) -> TransactionStatus:
    if command.is_both_ready():
        return TransactionStatus.OFF_CHAIN_READY
    elif command.is_abort():
        return TransactionStatus.CANCELED
    return default


def _new_payment_command_transaction(
    command: offchain.PaymentCommand, status: TransactionStatus
) -> Transaction:
    payment = command.payment
    sender_address, source_subaddress = _account_address_and_subaddress(
        payment.sender.address
    )
    destination_address, destination_subaddress = _account_address_and_subaddress(
        payment.receiver.address
    )
    source_id = get_account_id_from_subaddr(source_subaddress)
    destination_id = get_account_id_from_subaddr(destination_subaddress)

    return Transaction(
        type=TransactionType.OFFCHAIN,
        status=status,
        amount=payment.action.amount,
        currency=payment.action.currency,
        created_timestamp=datetime.utcnow(),
        source_id=source_id,
        source_address=sender_address,
        source_subaddress=source_subaddress,
        destination_id=destination_id,
        destination_address=destination_address,
        destination_subaddress=destination_subaddress,
        reference_id=command.reference_id(),
        command_json=offchain.to_json(command),
    )


def _account_address_and_subaddress(account_id: str) -> Tuple[str, Optional[str]]:
    account_address, sub = identifier.decode_account(
        account_id, context.get().config.diem_address_hrp()
    )
    return (account_address.to_hex(), sub.hex() if sub else None)


def _user_kyc_data(user_id: int) -> offchain.KycDataObject:
    return offchain.types.from_json_obj(
        kyc.get_user_kyc_info(user_id), offchain.KycDataObject, ""
    )


def _txn_payment_command(txn: Transaction) -> offchain.PaymentCommand:
    return offchain.from_json(txn.command_json, offchain.PaymentCommand)


def _offchain_client() -> offchain.Client:
    return context.get().offchain_client


def _compliance_private_key() -> Ed25519PrivateKey:
    return context.get().config.compliance_private_key()


def _hrp() -> str:
    return context.get().config.diem_address_hrp()


def get_payment_command_json(transaction_id: int) -> Optional[Dict]:
    transaction = storage.get_transaction(transaction_id)

    if transaction and transaction.command_json:
        return json.loads(transaction.command_json)

    return None


def get_account_payment_commands(account_id: int) -> List[Dict]:
    transactions = storage.get_account_transactions(account_id)
    commands = []

    for transaction in transactions:
        command_json = transaction.command_json

        if command_json:
            commands.append(json.loads(command_json))

    return commands


def get_funds_pull_pre_approvals(
    account_id: int,
) -> List[models.FundsPullPreApprovalCommand]:
    return get_account_commands(account_id)


def approve_funds_pull_pre_approval(
    funds_pull_pre_approval_id: str, status: str
) -> None:
    """ update command in db with new given status and role PAYER"""
    if status not in ["valid", "rejected"]:
        raise ValueError(f"Status must be 'valid' or 'rejected' and not '{status}'")

    command = get_funds_pull_pre_approval_command(funds_pull_pre_approval_id)

    if command:
        if command.status != "pending":
            raise RuntimeError(
                f"Could not approve command with status {command.status}"
            )
        update_command(funds_pull_pre_approval_id, status, Role.PAYER)
    else:
        raise RuntimeError(f"Could not find command {funds_pull_pre_approval_id}")


def establish_funds_pull_pre_approval(
    account_id: int,
    biller_address: str,
    funds_pull_pre_approval_id: str,
    funds_pull_pre_approval_type: str,
    expiration_timestamp: int,
    max_cumulative_unit: str = None,
    max_cumulative_unit_value: int = None,
    max_cumulative_amount: int = None,
    max_cumulative_amount_currency: str = None,
    max_transaction_amount: int = None,
    max_transaction_amount_currency: str = None,
    description: str = None,
) -> None:
    """ Establish funds pull pre approval by payer """
    validate_expiration_timestamp(expiration_timestamp)

    command = get_funds_pull_pre_approval_command(funds_pull_pre_approval_id)

    if command is not None:
        raise RuntimeError(
            f"Command with id {funds_pull_pre_approval_id} already exist in db"
        )

    vasp_address = context.get().config.vasp_address
    sub_address = account.generate_new_subaddress(account_id)
    hrp = context.get().config.diem_address_hrp()
    address = identifier.encode_account(vasp_address, sub_address, hrp)

    commit_command(
        models.FundsPullPreApprovalCommand(
            account_id=account_id,
            address=address,
            biller_address=biller_address,
            funds_pull_pre_approval_id=funds_pull_pre_approval_id,
            funds_pull_pre_approval_type=funds_pull_pre_approval_type,
            expiration_timestamp=expiration_timestamp,
            max_cumulative_unit=max_cumulative_unit,
            max_cumulative_unit_value=max_cumulative_unit_value,
            max_cumulative_amount=max_cumulative_amount,
            max_cumulative_amount_currency=max_cumulative_amount_currency,
            max_transaction_amount=max_transaction_amount,
            max_transaction_amount_currency=max_transaction_amount_currency,
            description=description,
            status=FundPullPreApprovalStatus.valid,
            role=Role.PAYER,
        )
    )


def preapproval_model_to_command(
    command: models.FundsPullPreApprovalCommand, my_address: str
):
    funds_pull_pre_approval = offchain.FundPullPreApprovalObject(
        funds_pull_pre_approval_id=command.funds_pull_pre_approval_id,
        address=command.address,
        biller_address=command.biller_address,
        scope=offchain.FundPullPreApprovalScopeObject(
            type=offchain.FundPullPreApprovalType.consent,
            expiration_timestamp=command.expiration_timestamp,
            max_cumulative_amount=offchain.ScopedCumulativeAmountObject(
                unit=command.max_cumulative_unit,
                value=command.max_cumulative_unit_value,
                max_amount=offchain.CurrencyObject(
                    amount=command.max_cumulative_amount,
                    currency=command.max_cumulative_amount_currency,
                ),
            ),
            max_transaction_amount=offchain.CurrencyObject(
                amount=command.max_transaction_amount,
                currency=command.max_transaction_amount_currency,
            ),
        ),
        status=command.status,
        description=command.description,
    )

    return offchain.FundsPullPreApprovalCommand(
        my_actor_address=my_address,
        funds_pull_pre_approval=funds_pull_pre_approval,
    )


def preapproval_command_to_model(
    account_id, command: offchain.FundsPullPreApprovalCommand, role: str
) -> models.FundsPullPreApprovalCommand:
    preapproval_object = command.funds_pull_pre_approval
    max_cumulative_amount = preapproval_object.scope.max_cumulative_amount
    max_transaction_amount = preapproval_object.scope.max_transaction_amount

    return models.FundsPullPreApprovalCommand(
        account_id=account_id,
        funds_pull_pre_approval_id=preapproval_object.funds_pull_pre_approval_id,
        address=preapproval_object.address,
        biller_address=preapproval_object.biller_address,
        funds_pull_pre_approval_type=preapproval_object.scope.type,
        expiration_timestamp=preapproval_object.scope.expiration_timestamp,
        max_cumulative_unit=max_cumulative_amount.unit
        if max_cumulative_amount
        else None,
        max_cumulative_unit_value=max_cumulative_amount.value
        if max_cumulative_amount
        else None,
        max_cumulative_amount=max_cumulative_amount.max_amount.amount
        if max_cumulative_amount
        else None,
        max_cumulative_amount_currency=max_cumulative_amount.max_amount.currency
        if max_cumulative_amount
        else None,
        max_transaction_amount=max_transaction_amount.amount
        if max_transaction_amount
        else None,
        max_transaction_amount_currency=max_transaction_amount.currency
        if max_transaction_amount
        else None,
        description=preapproval_object.description,
        status=preapproval_object.status,
        role=role,
    )


def validate_expiration_timestamp(expiration_timestamp):
    if expiration_timestamp < time.time():
        raise ValueError("expiration timestamp must be in the future")
