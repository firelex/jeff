// offline
use jeff_sdk::{Catalogue, ErrorType};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let catalogue = Catalogue::shipped()?;

    for reference in ["guard.atempt", "guard.attempt.extra", "guard"] {
        match catalogue.function(reference) {
            Err(error) if matches!(error.kind(), ErrorType::UnknownFunction | ErrorType::InvalidReference) => {
                println!("{} - {error}", error.type_name());
            }
            Err(error) => return Err(error.into()),
            Ok(_) => return Err(format!("{reference} named a function").into()),
        }
    }

    // Every Jeff error is a JeffError, whatever its kind(); request_id() is None for an error raised in this
    // program (a server's error carries the id of the request that failed).
    if let Err(error) = catalogue.function("guard.atempt") {
        println!("{} {:?}", error.type_name(), error.request_id());
    }

    Ok(())
}
