CREATE TABLE "sign_in_email" (
	"id" text PRIMARY KEY NOT NULL,
	"address_hash" text NOT NULL,
	"sent_at" bigint NOT NULL,
	CONSTRAINT "sign_in_email_hash_form" CHECK ("sign_in_email"."address_hash" ~ '^[0-9a-f]{64}$')
);
--> statement-breakpoint
CREATE INDEX "sign_in_email_address_sent" ON "sign_in_email" USING btree ("address_hash","sent_at");--> statement-breakpoint
CREATE INDEX "sign_in_email_sent" ON "sign_in_email" USING btree ("sent_at");