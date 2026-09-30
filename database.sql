-- MySQL dump 10.13  Distrib 26.7.0, for Win64 (x86_64)
--
-- Host: localhost    Database: queue_free_print
-- ------------------------------------------------------
-- Server version	26.7.0

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `admins`
--

DROP TABLE IF EXISTS `admins`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `admins` (
  `admin_id` int unsigned NOT NULL AUTO_INCREMENT,
  `name` varchar(150) NOT NULL,
  `email` varchar(255) NOT NULL,
  `phone` varchar(20) DEFAULT NULL,
  `password` varchar(255) NOT NULL,
  `shop_id` int unsigned DEFAULT NULL,
  PRIMARY KEY (`admin_id`),
  UNIQUE KEY `uq_admin_email` (`email`),
  UNIQUE KEY `uq_admin_shop` (`shop_id`),
  KEY `idx_admin_shop` (`shop_id`),
  CONSTRAINT `fk_admin_shop` FOREIGN KEY (`shop_id`) REFERENCES `print_shops` (`shop_id`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `admins`
--

LOCK TABLES `admins` WRITE;
/*!40000 ALTER TABLE `admins` DISABLE KEYS */;
INSERT INTO `admins` VALUES (1,'Admin User','admin@test.com',NULL,'TEMP_ADMIN_PASSWORD',1);
/*!40000 ALTER TABLE `admins` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `document_analysis`
--

DROP TABLE IF EXISTS `document_analysis`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `document_analysis` (
  `analysis_id` int unsigned NOT NULL AUTO_INCREMENT,
  `order_id` int unsigned NOT NULL,
  `blank_pages` int unsigned NOT NULL DEFAULT '0',
  `duplicate_pages` int unsigned NOT NULL DEFAULT '0',
  `invisible_text` tinyint(1) NOT NULL DEFAULT '0',
  `total_pages` int unsigned NOT NULL DEFAULT '0',
  `blurry_pages` int unsigned NOT NULL DEFAULT '0',
  `unrecognizable_pages` int unsigned NOT NULL DEFAULT '0',
  `analysis_status` enum('pending','completed','failed') NOT NULL DEFAULT 'pending',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`analysis_id`),
  KEY `fk_analysis_order` (`order_id`),
  CONSTRAINT `fk_analysis_order` FOREIGN KEY (`order_id`) REFERENCES `orders` (`order_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `document_analysis`
--

LOCK TABLES `document_analysis` WRITE;
/*!40000 ALTER TABLE `document_analysis` DISABLE KEYS */;
INSERT INTO `document_analysis` VALUES (1,1,1,2,0,'completed','2026-09-02 18:25:42');
/*!40000 ALTER TABLE `document_analysis` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `login_accounts`
--

DROP TABLE IF EXISTS `login_accounts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `login_accounts` (
  `login_id` int unsigned NOT NULL AUTO_INCREMENT,
  `registration_id` int unsigned NOT NULL,
  `password` varchar(255) NOT NULL,
  `role` enum('user','admin') NOT NULL DEFAULT 'user',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`login_id`),
  UNIQUE KEY `unique_login_registration` (`registration_id`),
  CONSTRAINT `fk_login_registration` FOREIGN KEY (`registration_id`) REFERENCES `registrations` (`registration_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `login_accounts`
--

LOCK TABLES `login_accounts` WRITE;
/*!40000 ALTER TABLE `login_accounts` DISABLE KEYS */;
INSERT INTO `login_accounts` VALUES (1,1,'TEMP_ADMIN_PASSWORD','admin','2026-09-03 07:31:33'),(2,3,'TEMP_USER_PASSWORD','user','2026-09-03 07:31:33'),(3,2,'TEMP_USER_PASSWORD','user','2026-09-03 07:31:33'),(4,4,'Amit@123','user','2026-09-05 12:09:19');
/*!40000 ALTER TABLE `login_accounts` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `notifications`
--

DROP TABLE IF EXISTS `notifications`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `notifications` (
  `notification_id` int unsigned NOT NULL AUTO_INCREMENT,
  `user_id` int unsigned NOT NULL,
  `order_id` int unsigned DEFAULT NULL,
  `message` text NOT NULL,
  `notification_type` enum('order','payment','system') NOT NULL DEFAULT 'order',
  `is_read` tinyint(1) NOT NULL DEFAULT '0',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`notification_id`),
  KEY `fk_notification_user` (`user_id`),
  KEY `fk_notification_order` (`order_id`),
  CONSTRAINT `fk_notification_order` FOREIGN KEY (`order_id`) REFERENCES `orders` (`order_id`) ON DELETE SET NULL ON UPDATE CASCADE,
  CONSTRAINT `fk_notification_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `notifications`
--

LOCK TABLES `notifications` WRITE;
/*!40000 ALTER TABLE `notifications` DISABLE KEYS */;
/*!40000 ALTER TABLE `notifications` ENABLE KEYS */;
UNLOCK TABLES;

--
-- ============================================================
-- Web Push subscriptions
-- ============================================================
DROP TABLE IF EXISTS `push_subscriptions`;
CREATE TABLE `push_subscriptions` (
  `subscription_id` int unsigned NOT NULL AUTO_INCREMENT,
  `user_id` int unsigned NOT NULL,
  `endpoint` text NOT NULL,
  `p256dh` varchar(255) NOT NULL,
  `auth` varchar(255) NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`subscription_id`),
  UNIQUE KEY `uq_push_endpoint` (`endpoint`(255)),
  KEY `idx_push_user` (`user_id`),
  CONSTRAINT `fk_push_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Table structure for table `order_history`
--

DROP TABLE IF EXISTS `order_history`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `order_history` (
  `history_id` int unsigned NOT NULL AUTO_INCREMENT,
  `order_id` int unsigned NOT NULL,
  `old_status` enum('pending','accepted','printing','ready','completed','declined','cancelled','delivered') DEFAULT NULL,
  `new_status` enum('pending','accepted','printing','ready','completed','declined','cancelled','delivered') NOT NULL,
  `changed_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`history_id`),
  KEY `fk_history_order` (`order_id`),
  CONSTRAINT `fk_history_order` FOREIGN KEY (`order_id`) REFERENCES `orders` (`order_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `order_history`
--

LOCK TABLES `order_history` WRITE;
/*!40000 ALTER TABLE `order_history` DISABLE KEYS */;
INSERT INTO `order_history` VALUES (1,1,'pending','accepted','2026-09-02 18:26:28');
/*!40000 ALTER TABLE `order_history` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `order_items`
--

DROP TABLE IF EXISTS `order_items`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `order_items` (
  `item_id` int unsigned NOT NULL AUTO_INCREMENT,
  `order_id` int unsigned NOT NULL,
  `file_name` varchar(255) NOT NULL,
  `file_path` varchar(500) NOT NULL,
  `copies` int unsigned NOT NULL DEFAULT '1',
  `color` enum('black_white','color') NOT NULL DEFAULT 'black_white',
  `double_sided` tinyint(1) NOT NULL DEFAULT '0',
  `page_range` varchar(100) NOT NULL DEFAULT 'all',
  `cost` decimal(10,2) NOT NULL DEFAULT '0.00',
  `shop_id` int unsigned DEFAULT NULL,
  `orientation` varchar(30) DEFAULT NULL,
  `print_side` varchar(30) DEFAULT NULL,
  `paper_type` varchar(50) DEFAULT NULL,
  `additional_requirements` text,
  `total_pages` int unsigned DEFAULT NULL,
  `blank_pages` int unsigned DEFAULT NULL,
  `blurry_pages` int unsigned DEFAULT NULL,
  `unrecognizable_pages` int unsigned DEFAULT NULL,
  `uploaded_at` datetime DEFAULT NULL,
  PRIMARY KEY (`item_id`),
  KEY `idx_item_order` (`order_id`),
  CONSTRAINT `fk_item_order` FOREIGN KEY (`order_id`) REFERENCES `orders` (`order_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `order_items`
--

LOCK TABLES `order_items` WRITE;
/*!40000 ALTER TABLE `order_items` DISABLE KEYS */;
INSERT INTO `order_items` VALUES (1,1,'assignment.pdf','uploads/assignment.pdf',2,'black_white',1,'1-10',20.00),(2,2,'assignment.pdf','uploads/assignment.pdf',2,'black_white',1,'1-10',20.00);
/*!40000 ALTER TABLE `order_items` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `orders`
--

DROP TABLE IF EXISTS `orders`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `orders` (
  `order_id` int unsigned NOT NULL AUTO_INCREMENT,
  `user_id` int unsigned NOT NULL,
  `shop_id` int unsigned NOT NULL,
  `document` varchar(255) NOT NULL,
  `copies` int unsigned NOT NULL DEFAULT '1',
  `color` enum('black_white','color') NOT NULL DEFAULT 'black_white',
  `orientation` enum('portrait','landscape') NOT NULL DEFAULT 'portrait',
  `print_side` enum('single','double') NOT NULL DEFAULT 'single',
  `page_range` varchar(100) NOT NULL DEFAULT 'all',
  `paper_type` varchar(50) NOT NULL DEFAULT 'A4',
  `additional_requirements` text,
  `estimated_cost` decimal(10,2) NOT NULL DEFAULT '0.00',
  `order_status` enum('pending','accepted','printing','ready','completed','declined','cancelled','delivered') NOT NULL DEFAULT 'pending',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `order_date` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `total_cost` decimal(10,2) NOT NULL DEFAULT '0.00',
  `notification_sent` tinyint(1) NOT NULL DEFAULT '0',
  UNIQUE KEY `uq_orders_delivery_token` (`delivery_token`),
  `delivery_token` varchar(128) DEFAULT NULL,
  `delivery_pdf_path` varchar(500) DEFAULT NULL,
  `delivered_at` timestamp NULL DEFAULT NULL,
  `edit_count` tinyint unsigned NOT NULL DEFAULT '0',
  `last_edited_at` timestamp NULL DEFAULT NULL,
  PRIMARY KEY (`order_id`),
  KEY `fk_order_user` (`user_id`),
  KEY `fk_order_shop` (`shop_id`),
  CONSTRAINT `fk_order_shop` FOREIGN KEY (`shop_id`) REFERENCES `print_shops` (`shop_id`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `fk_order_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `orders`
--

LOCK TABLES `orders` WRITE;
/*!40000 ALTER TABLE `orders` DISABLE KEYS */;
INSERT INTO `orders` VALUES (1,2,1,'assignment.pdf',2,'black_white','portrait','double','1-10','A4','Please staple the pages',20.00,'accepted','2026-09-02 18:22:17','2026-09-02 18:22:17',20.00,0),(2,2,1,'assignment.pdf',2,'black_white','portrait','double','1-10','A4','Please staple the pages',20.00,'pending','2026-09-02 18:24:07','2026-09-02 18:24:07',20.00,0);
/*!40000 ALTER TABLE `orders` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `payments`
--

DROP TABLE IF EXISTS `payments`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `payments` (
  `payment_id` int unsigned NOT NULL AUTO_INCREMENT,
  `order_id` int unsigned NOT NULL,
  `amount` decimal(10,2) NOT NULL,
  `payment_status` enum('pending','paid','failed','refunded') NOT NULL DEFAULT 'pending',
  `payment_method` varchar(30) DEFAULT NULL,
  `razorpay_payment_id` varchar(100) DEFAULT NULL,
  `razorpay_order_id` varchar(100) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`payment_id`),
  KEY `fk_payment_order` (`order_id`),
  CONSTRAINT `fk_payment_order` FOREIGN KEY (`order_id`) REFERENCES `orders` (`order_id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `payments`
--

LOCK TABLES `payments` WRITE;
/*!40000 ALTER TABLE `payments` DISABLE KEYS */;
INSERT INTO `payments` (`payment_id`,`order_id`,`amount`,`payment_status`,`payment_method`,`razorpay_payment_id`,`razorpay_order_id`,`created_at`) VALUES (1,1,20.00,'paid','upi','pay_test_001','order_test_001','2026-09-02 18:24:41');
/*!40000 ALTER TABLE `payments` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `print_settings`
--

DROP TABLE IF EXISTS `print_settings`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `print_settings` (
  `setting_id` int unsigned NOT NULL AUTO_INCREMENT,
  `order_id` int unsigned NOT NULL,
  `copies` int unsigned NOT NULL DEFAULT '1',
  `color` enum('black_white','color') NOT NULL DEFAULT 'black_white',
  `double_sided` tinyint(1) NOT NULL DEFAULT '0',
  `page_range` varchar(100) NOT NULL DEFAULT 'all',
  `cost` decimal(10,2) NOT NULL DEFAULT '0.00',
  PRIMARY KEY (`setting_id`),
  UNIQUE KEY `uq_print_settings_order` (`order_id`),
  KEY `idx_settings_order` (`order_id`),
  CONSTRAINT `fk_settings_order` FOREIGN KEY (`order_id`) REFERENCES `orders` (`order_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=6 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `print_settings`
--

LOCK TABLES `print_settings` WRITE;
/*!40000 ALTER TABLE `print_settings` DISABLE KEYS */;
INSERT INTO `print_settings` VALUES (1,1,2,'black_white',1,'1-10',20.00),(2,2,2,'black_white',1,'1-10',20.00);
/*!40000 ALTER TABLE `print_settings` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `print_shops`
--

DROP TABLE IF EXISTS `print_shops`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `print_shops` (
  `shop_id` int unsigned NOT NULL AUTO_INCREMENT,
  `shop_name` varchar(150) NOT NULL,
  `shop_number` varchar(50) NOT NULL,
  `university_name` varchar(200) DEFAULT NULL,
  `state` varchar(100) DEFAULT NULL,
  `gst_number` varchar(15) DEFAULT NULL,
  `address` varchar(300) DEFAULT NULL,
  `latitude` decimal(10,7) DEFAULT NULL,
  `longitude` decimal(10,7) DEFAULT NULL,
  `owner_id` int unsigned NOT NULL,
  `is_open` tinyint(1) NOT NULL DEFAULT '1',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`shop_id`),
  UNIQUE KEY `shop_number` (`shop_number`),
  KEY `fk_shop_owner` (`owner_id`),
  CONSTRAINT `fk_shop_owner` FOREIGN KEY (`owner_id`) REFERENCES `users` (`user_id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=6 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `print_shops`
--

LOCK TABLES `print_shops` WRITE;
/*!40000 ALTER TABLE `print_shops` DISABLE KEYS */;
INSERT INTO `print_shops` VALUES (1,'College Print Shop','SHOP-001','Guru Jambheshwar University','Haryana',NULL,'',1,1,'2026-09-02 18:21:42'),(3,'Library Print Shop','SHOP-002','Guru Jambheshwar University','Haryana',NULL,'',1,1,'2026-09-08 08:35:56'),(4,'Main Gate Print Shop','SHOP-003','Guru Jambheshwar University','Haryana',NULL,'',1,1,'2026-09-08 08:44:33');
/*!40000 ALTER TABLE `print_shops` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `print_prices`
--

DROP TABLE IF EXISTS `print_prices`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `print_prices` (
  `price_id` int NOT NULL AUTO_INCREMENT,
  `shop_id` int unsigned NOT NULL,
  `print_type` varchar(50) NOT NULL,
  `min_pages` int NOT NULL,
  `max_pages` int NOT NULL,
  `price_per_page` decimal(10,2) NOT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`price_id`),
  KEY `shop_id` (`shop_id`),
  CONSTRAINT `print_prices_ibfk_1` FOREIGN KEY (`shop_id`) REFERENCES `print_shops` (`shop_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `printers`
--

DROP TABLE IF EXISTS `printers`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `printers` (
  `printer_id` int unsigned NOT NULL AUTO_INCREMENT,
  `name` varchar(150) NOT NULL,
  `shop_id` int unsigned NOT NULL,
  `type` varchar(50) NOT NULL,
  `status` enum('available','busy','offline') NOT NULL DEFAULT 'available',
  PRIMARY KEY (`printer_id`),
  KEY `idx_printer_shop` (`shop_id`),
  CONSTRAINT `fk_printer_shop` FOREIGN KEY (`shop_id`) REFERENCES `print_shops` (`shop_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `printers`
--

LOCK TABLES `printers` WRITE;
/*!40000 ALTER TABLE `printers` DISABLE KEYS */;
INSERT INTO `printers` VALUES (1,'College Printer 1',1,'Laser','available');
/*!40000 ALTER TABLE `printers` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `registrations`
--

DROP TABLE IF EXISTS `registrations`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `registrations` (
  `registration_id` int unsigned NOT NULL AUTO_INCREMENT,
  `name` varchar(100) NOT NULL,
  `email` varchar(150) NOT NULL,
  `registered_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `role` enum('user','admin') NOT NULL DEFAULT 'user',
  PRIMARY KEY (`registration_id`),
  UNIQUE KEY `unique_registration_email` (`email`)
) ENGINE=InnoDB AUTO_INCREMENT=6 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `registrations`
--

LOCK TABLES `registrations` WRITE;
/*!40000 ALTER TABLE `registrations` DISABLE KEYS */;
INSERT INTO `registrations` VALUES (1,'Admin User','admin@test.com','2026-09-03 07:31:11','admin'),(2,'Rahul Kumar','rahul@test.com','2026-09-03 07:31:11','user'),(3,'Priya Sharma','priya@test.com','2026-09-03 07:31:11','user'),(4,'Amit Sharma','amit@test.com','2026-09-05 12:09:19','user');
/*!40000 ALTER TABLE `registrations` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `users`
--

DROP TABLE IF EXISTS `users`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `users` (
  `user_id` int unsigned NOT NULL AUTO_INCREMENT,
  `name` varchar(100) NOT NULL,
  `email` varchar(150) NOT NULL,
  `phone` varchar(20) DEFAULT NULL,
  `password` varchar(255) NOT NULL,
  `role` enum('user','admin') NOT NULL DEFAULT 'user',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`),
  UNIQUE KEY `email` (`email`)
) ENGINE=InnoDB AUTO_INCREMENT=11 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `users`
--

LOCK TABLES `users` WRITE;
/*!40000 ALTER TABLE `users` DISABLE KEYS */;
INSERT INTO `users` VALUES (1,'Admin User','admin@test.com','TEMP_ADMIN_PASSWORD','admin','2026-09-02 18:21:30'),(2,'Rahul Kumar','rahul@test.com','TEMP_USER_PASSWORD','user','2026-09-02 18:21:30'),(3,'Priya Sharma','priya@test.com','TEMP_USER_PASSWORD','user','2026-09-02 18:21:30'),(9,'Amit Sharma','amit@test.com','Amit@123','user','2026-09-05 12:09:19');
/*!40000 ALTER TABLE `users` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-09-09 12:11:07


-- ============================================================
-- RAZORPAY PAYMENT GATEWAY
-- Credentials are intentionally NOT included in this SQL dump.
-- Enter them from Admin > Settings > Payment Gateway.
-- ============================================================
CREATE TABLE IF NOT EXISTS `payment_gateway_settings` (
  `setting_id` int unsigned NOT NULL AUTO_INCREMENT,
  `shop_id` int unsigned NOT NULL,
  `provider` varchar(30) NOT NULL DEFAULT 'razorpay',
  `key_id` varchar(150) NOT NULL,
  `key_secret` varchar(255) NOT NULL,
  `is_enabled` tinyint(1) NOT NULL DEFAULT 1,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`setting_id`),
  UNIQUE KEY `uq_payment_shop_provider` (`shop_id`,`provider`),
  KEY `fk_payment_gateway_shop` (`shop_id`),
  CONSTRAINT `fk_payment_gateway_shop` FOREIGN KEY (`shop_id`) REFERENCES `print_shops` (`shop_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE `payments` MODIFY COLUMN `payment_method` varchar(30) DEFAULT NULL;
ALTER TABLE `payments` ADD COLUMN IF NOT EXISTS `razorpay_signature` varchar(255) DEFAULT NULL AFTER `razorpay_order_id`;
ALTER TABLE `payments` ADD COLUMN IF NOT EXISTS `razorpay_qr_id` varchar(100) DEFAULT NULL AFTER `razorpay_signature`;
ALTER TABLE `payments` ADD COLUMN IF NOT EXISTS `razorpay_qr_image_url` varchar(500) DEFAULT NULL AFTER `razorpay_qr_id`;
ALTER TABLE `payments` ADD COLUMN IF NOT EXISTS `razorpay_qr_status` varchar(30) DEFAULT NULL AFTER `razorpay_qr_image_url`;


-- Nearby shop ratings/reviews (QueueFree only)
CREATE TABLE IF NOT EXISTS `shop_reviews` (
  `review_id` int unsigned NOT NULL AUTO_INCREMENT,
  `shop_id` int unsigned NOT NULL,
  `user_id` int unsigned NOT NULL,
  `rating` tinyint unsigned NOT NULL,
  `review_text` varchar(500) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`review_id`),
  UNIQUE KEY `uq_shop_user_review` (`shop_id`,`user_id`),
  KEY `idx_shop_reviews_shop` (`shop_id`),
  CONSTRAINT `fk_shop_reviews_shop` FOREIGN KEY (`shop_id`) REFERENCES `print_shops` (`shop_id`) ON DELETE CASCADE,
  CONSTRAINT `fk_shop_reviews_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
