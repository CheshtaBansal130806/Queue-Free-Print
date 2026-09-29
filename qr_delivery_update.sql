-- QueueFree Secure QR-Based Printout Delivery
-- Run this once on an existing queue_free_print database.

ALTER TABLE orders
  MODIFY order_status ENUM('pending','accepted','printing','ready','completed','declined','cancelled','delivered')
  NOT NULL DEFAULT 'pending';

ALTER TABLE order_history
  MODIFY old_status ENUM('pending','accepted','printing','ready','completed','declined','cancelled','delivered') DEFAULT NULL,
  MODIFY new_status ENUM('pending','accepted','printing','ready','completed','declined','cancelled','delivered') NOT NULL;

ALTER TABLE orders
  ADD COLUMN delivery_token VARCHAR(128) NULL,
  ADD COLUMN delivery_pdf_path VARCHAR(500) NULL,
  ADD COLUMN delivered_at TIMESTAMP NULL,
  ADD UNIQUE KEY uq_orders_delivery_token (delivery_token);
