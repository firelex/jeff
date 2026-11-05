-- Every request sent before data sets existed gave typed examples: it is "generate". The default exists only for
-- those rows and is dropped at once, so a new row must say its source.
ALTER TABLE "adapter_request" ADD COLUMN "example_source" text NOT NULL DEFAULT 'generate';--> statement-breakpoint
ALTER TABLE "adapter_request" ALTER COLUMN "example_source" DROP DEFAULT;--> statement-breakpoint
ALTER TABLE "adapter_request" ADD COLUMN "data_set_pathname" text;--> statement-breakpoint
ALTER TABLE "adapter_request" ADD COLUMN "data_set_url" text;--> statement-breakpoint
ALTER TABLE "adapter_request" ADD COLUMN "data_set_bytes" integer;--> statement-breakpoint
ALTER TABLE "adapter_request" ADD COLUMN "data_set_rows" integer;--> statement-breakpoint
ALTER TABLE "adapter_request" ADD COLUMN "data_set_counts" jsonb;--> statement-breakpoint
ALTER TABLE "adapter_request" ADD CONSTRAINT "adapter_request_data_set_pathname_unique" UNIQUE("data_set_pathname");--> statement-breakpoint
ALTER TABLE "adapter_request" ADD CONSTRAINT "adapter_request_example_source" CHECK ("adapter_request"."example_source" in ('data_set', 'generate'));--> statement-breakpoint
ALTER TABLE "adapter_request" ADD CONSTRAINT "adapter_request_data_set" CHECK (("adapter_request"."example_source" = 'data_set') = ("adapter_request"."data_set_pathname" is not null)
    and ("adapter_request"."data_set_pathname" is null) = ("adapter_request"."data_set_url" is null) and ("adapter_request"."data_set_pathname" is null) = ("adapter_request"."data_set_bytes" is null)
    and ("adapter_request"."data_set_pathname" is null) = ("adapter_request"."data_set_rows" is null) and ("adapter_request"."data_set_pathname" is null) = ("adapter_request"."data_set_counts" is null));