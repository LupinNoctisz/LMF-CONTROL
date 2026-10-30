from datetime import datetime, date
from sqlalchemy import String, Integer, Float, DateTime, Date, ForeignKey, Text, Boolean, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base

class Timestamped(Base):
    __abstract__ = True
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

class Product(Timestamped):
    __tablename__ = 'products'
    sku: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    internal_code: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    category: Mapped[str | None] = mapped_column(String(100), index=True)
    supplier: Mapped[str | None] = mapped_column(String(200), index=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey('suppliers.id'), index=True)
    brand: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    min_quantity: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    price: Mapped[float] = mapped_column(Float, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

class Movement(Timestamped):
    __tablename__ = 'movements'
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(250))
    reference: Mapped[str | None] = mapped_column(String(120))
    source_key: Mapped[str | None] = mapped_column(String(180), unique=True, index=True)
    __table_args__ = (CheckConstraint('quantity > 0', name='ck_movement_positive_quantity'),)

class Customer(Timestamped):
    __tablename__ = 'customers'
    name: Mapped[str] = mapped_column(String(200), index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    whatsapp: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    document: Mapped[str | None] = mapped_column(String(30), index=True)
    public_seq: Mapped[int | None] = mapped_column(Integer, unique=True, index=True)  # nº público sequencial (01, 02...)
    address: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class Supplier(Timestamped):
    __tablename__ = 'suppliers'
    name: Mapped[str] = mapped_column(String(200), index=True)
    contact: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    whatsapp: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(250))
    categories: Mapped[str | None] = mapped_column(String(300))
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

class BankAccount(Timestamped):
    __tablename__ = 'bank_accounts'
    name: Mapped[str] = mapped_column(String(120), index=True)
    business_unit: Mapped[str] = mapped_column(String(80), default='Assistência')
    provider: Mapped[str] = mapped_column(String(100), default='InfinitePay')
    opening_balance: Mapped[float] = mapped_column(Float, default=0)
    opening_balance_date: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

class AppUser(Timestamped):
    __tablename__ = 'app_users'
    username: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    recovery_code_hash: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class AccountBalanceAdjustment(Timestamped):
    __tablename__ = 'account_balance_adjustments'
    bank_account_id: Mapped[int] = mapped_column(ForeignKey('bank_accounts.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('app_users.id'), index=True)
    old_balance: Mapped[float] = mapped_column(Float)
    new_balance: Mapped[float] = mapped_column(Float)
    old_reference_date: Mapped[date | None] = mapped_column(Date)
    new_reference_date: Mapped[date | None] = mapped_column(Date)
    action: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(250))

class ServiceOrder(Timestamped):
    __tablename__ = 'service_orders'
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey('customers.id'))
    equipment: Mapped[str] = mapped_column(String(200))
    serial: Mapped[str | None] = mapped_column(String(100))  # texto: preserva zeros à esquerda
    equipment_type: Mapped[str | None] = mapped_column(String(20), index=True)
    accessories: Mapped[str | None] = mapped_column(Text)
    physical_condition: Mapped[str | None] = mapped_column(Text)
    reported_fault: Mapped[str] = mapped_column(Text)
    diagnosis: Mapped[str | None] = mapped_column(Text)
    service_executed: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default='Recebido', index=True)
    estimate: Mapped[float] = mapped_column(Float, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    charged: Mapped[float] = mapped_column(Float, default=0)
    approval: Mapped[str] = mapped_column(String(20), default='Pendente')
    entered_on: Mapped[date] = mapped_column(Date, default=date.today)
    due_on: Mapped[date | None] = mapped_column(Date)
    delivered_on: Mapped[date | None] = mapped_column(Date)
    completed_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

class Order(Timestamped):
    __tablename__ = 'orders'
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), default='cliente')
    customer_id: Mapped[int | None] = mapped_column(ForeignKey('customers.id'))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey('suppliers.id'))
    description: Mapped[str] = mapped_column(String(250))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    total: Mapped[float] = mapped_column(Float, default=0)
    paid: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(40), default='Solicitação recebida', index=True)
    due_on: Mapped[date | None] = mapped_column(Date)
    received: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

class OrderItem(Timestamped):
    __tablename__ = 'order_items'
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id', ondelete='CASCADE'), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    discount: Mapped[float] = mapped_column(Float, default=0)
    received_quantity: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (CheckConstraint('quantity > 0', name='ck_order_item_positive_quantity'),)

class PurchaseReceipt(Timestamped):
    __tablename__ = 'purchase_receipts'
    request_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id'), index=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey('order_items.id'), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[float] = mapped_column(Float, default=0)
    __table_args__ = (CheckConstraint('quantity > 0', name='ck_purchase_receipt_positive_quantity'),)

class Guarantee(Timestamped):
    __tablename__ = 'guarantees'
    customer_id: Mapped[int] = mapped_column(ForeignKey('customers.id'))
    service_order_id: Mapped[int | None] = mapped_column(ForeignKey('service_orders.id'))
    item: Mapped[str] = mapped_column(String(200))
    guarantee_type: Mapped[str] = mapped_column(String(40), default='serviço')
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    duration_days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default='ativa', index=True)
    conditions: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

class GuaranteeOccurrence(Timestamped):
    __tablename__ = 'guarantee_occurrences'
    guarantee_id: Mapped[int] = mapped_column(ForeignKey('guarantees.id'), index=True)
    opened_on: Mapped[date] = mapped_column(Date, default=date.today)
    issue: Mapped[str] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default='aberta')
    closed_on: Mapped[date | None] = mapped_column(Date)

class Counter(Timestamped):
    """Contadores persistentes (nunca reutilizam números já emitidos)."""
    __tablename__ = 'counters'
    name: Mapped[str] = mapped_column(String(60), unique=True)
    value: Mapped[int] = mapped_column(Integer, default=0)

class ServiceOrderPhoto(Timestamped):
    """Foto anexada a uma ordem de serviço. O arquivo fica em DATA_DIR/documentos (incluído nos backups)."""
    __tablename__ = 'service_order_photos'
    service_order_id: Mapped[int] = mapped_column(ForeignKey('service_orders.id'), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(80), unique=True)
    content_type: Mapped[str] = mapped_column(String(40))
    size: Mapped[int] = mapped_column(Integer, default=0)

class ServiceOrderEvent(Timestamped):
    __tablename__ = 'service_order_events'
    service_order_id: Mapped[int] = mapped_column(ForeignKey('service_orders.id', ondelete='CASCADE'), index=True)
    old_status: Mapped[str | None] = mapped_column(String(40))
    new_status: Mapped[str] = mapped_column(String(40))
    details: Mapped[str | None] = mapped_column(Text)

class ServicePart(Timestamped):
    __tablename__ = 'service_parts'
    service_order_id: Mapped[int] = mapped_column(ForeignKey('service_orders.id'), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[float] = mapped_column(Float, default=0)
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    source_key: Mapped[str] = mapped_column(String(180), unique=True)
    __table_args__ = (CheckConstraint('quantity > 0', name='ck_service_part_positive_quantity'),)

class Sale(Timestamped):
    __tablename__ = 'sales'
    number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    request_key: Mapped[str | None] = mapped_column(String(180), unique=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey('customers.id'), index=True)
    sold_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    status: Mapped[str] = mapped_column(String(30), default='confirmada', index=True)
    total: Mapped[float] = mapped_column(Float, default=0)
    discount: Mapped[float] = mapped_column(Float, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0)
    received_amount: Mapped[float] = mapped_column(Float, default=0)
    payment_method: Mapped[str | None] = mapped_column(String(50))
    bank_account_id: Mapped[int | None] = mapped_column(ForeignKey('bank_accounts.id'))
    notes: Mapped[str | None] = mapped_column(Text)

class SaleItem(Timestamped):
    __tablename__ = 'sale_items'
    sale_id: Mapped[int] = mapped_column(ForeignKey('sales.id', ondelete='CASCADE'), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float, default=0)
    discount: Mapped[float] = mapped_column(Float, default=0)
    __table_args__ = (CheckConstraint('quantity > 0', name='ck_sale_item_positive_quantity'),)

class FinancialEntry(Timestamped):
    __tablename__ = 'financial_entries'
    kind: Mapped[str] = mapped_column(String(20), index=True)
    category: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(250))
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default='pendente', index=True)
    competence_on: Mapped[date | None] = mapped_column(Date)
    due_on: Mapped[date | None] = mapped_column(Date)
    paid_on: Mapped[date | None] = mapped_column(Date)
    payment_method: Mapped[str | None] = mapped_column(String(40))
    bank_account_id: Mapped[int | None] = mapped_column(ForeignKey('bank_accounts.id'), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey('customers.id'), index=True)
    service_order_id: Mapped[int | None] = mapped_column(ForeignKey('service_orders.id'), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey('orders.id'), index=True)
    sale_id: Mapped[int | None] = mapped_column(ForeignKey('sales.id'), index=True)
    external_id: Mapped[str | None] = mapped_column(String(180), unique=True, index=True)
    reference: Mapped[str | None] = mapped_column(String(120), unique=True)

class BankTransaction(Timestamped):
    __tablename__ = 'bank_transactions'
    bank_account_id: Mapped[int] = mapped_column(ForeignKey('bank_accounts.id'), index=True)
    external_id: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    occurred_on: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String(250))
    kind: Mapped[str] = mapped_column(String(20))
    amount: Mapped[float] = mapped_column(Float)
    financial_entry_id: Mapped[int | None] = mapped_column(ForeignKey('financial_entries.id'))
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False)

class ImportLog(Timestamped):
    __tablename__ = 'import_logs'
    filename: Mapped[str] = mapped_column(String(250))
    result: Mapped[str] = mapped_column(Text)


class AuditLog(Timestamped):
    """Histórico de auditoria somente de acréscimo: nenhuma rota altera ou apaga estas linhas.
    Nunca guarda senhas, tokens nem hashes (apenas quem, o quê, quando, de onde e o resultado)."""
    __tablename__ = 'audit_log'
    user_id: Mapped[int | None] = mapped_column(ForeignKey('app_users.id'), index=True)
    username: Mapped[str] = mapped_column(String(60), default='sistema')
    action: Mapped[str] = mapped_column(String(80), index=True)
    target: Mapped[str | None] = mapped_column(String(120))
    outcome: Mapped[str] = mapped_column(String(20), index=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[str | None] = mapped_column(String(500))


class OperationAuthorization(Timestamped):
    """Autorização administrativa de USO ÚNICO para uma operação destrutiva sobre um alvo específico.
    Só o hash SHA-256 do token é guardado; o token em si existe apenas na resposta à emissão."""
    __tablename__ = 'operation_authorizations'
    user_id: Mapped[int] = mapped_column(ForeignKey('app_users.id'), index=True)
    operation: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    ip: Mapped[str | None] = mapped_column(String(64))
