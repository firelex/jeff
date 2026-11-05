CREATE TABLE "stripe_customer" (
	"user_id" text PRIMARY KEY NOT NULL,
	"customer_id" text NOT NULL,
	"created_at" timestamp with time zone NOT NULL,
	CONSTRAINT "stripe_customer_customer_id_unique" UNIQUE("customer_id")
);
--> statement-breakpoint
ALTER TABLE "stripe_customer" ADD CONSTRAINT "stripe_customer_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;