CREATE TABLE "adapter_request" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"status" text NOT NULL,
	"form" jsonb NOT NULL,
	"examples" jsonb NOT NULL,
	"may_share" boolean NOT NULL,
	"no_personal_data" boolean NOT NULL,
	"may_improve_base" boolean NOT NULL,
	"created_at" timestamp with time zone NOT NULL,
	"updated_at" timestamp with time zone NOT NULL,
	"decline_reason" text,
	CONSTRAINT "adapter_request_status" CHECK ("adapter_request"."status" in ('received', 'in_review', 'training', 'delivered', 'declined')),
	CONSTRAINT "adapter_request_decline_reason" CHECK (("adapter_request"."status" = 'declined') = ("adapter_request"."decline_reason" is not null))
);
--> statement-breakpoint
ALTER TABLE "adapter_request" ADD CONSTRAINT "adapter_request_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "adapter_request_user_created" ON "adapter_request" USING btree ("user_id","created_at");--> statement-breakpoint
CREATE INDEX "adapter_request_created" ON "adapter_request" USING btree ("created_at");